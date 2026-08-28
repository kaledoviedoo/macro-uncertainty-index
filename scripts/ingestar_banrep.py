"""
ingestar_banrep.py — Series colombianas desde su fuente primaria.

Sustituye a ingestar_colcap.py, que solo sabía traer un índice. Ahora recorre
todos los activos con `fuente_datos = 'banrep_sdmx'` y usa el dataflow que
cada uno declara en `activos.serie_externa`. Añadir una serie es insertar una
fila en la tabla, no editar este archivo.

    Endpoint: https://totoro.banrep.gov.co/nsi-jax-ws/rest
    Formato:  SDMX-ML 2.1 (XML)

Series cargadas hoy:
    TRM     COP/USD                      diaria    DF_TRM_DAILY_HIST
    CBR     Tasa de política Banrep      diaria    DF_CBR_DAILY_HIST
    IBR     Interbancaria overnight      diaria    DF_IBR_DAILY_HIST
    COLCAP  Índice accionario            mensual   DF_COLCAP_MONTHLY_HIST

Uso:
    python scripts/ingestar_banrep.py                    # explora, no escribe
    python scripts/ingestar_banrep.py --escribir
    python scripts/ingestar_banrep.py --escribir --solo TRM
    python scripts/ingestar_banrep.py --catalogo         # lista los dataflows
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from calendar import monthrange
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from comun import PERFILES_UA, conectar, log, upsert

BASE = "https://totoro.banrep.gov.co/nsi-jax-ws/rest"
AGENCIA = "ESTAT"


# --------------------------------------------------------------------------
def pedir(url: str, timeout: int = 90,
          silencioso: bool = False) -> requests.Response | None:
    """Prueba los perfiles de cabecera hasta que uno devuelva 200."""
    for perfil, cabeceras in PERFILES_UA.items():
        try:
            h = dict(cabeceras)
            h["Accept"] = ("application/vnd.sdmx.genericdata+xml;version=2.1, "
                           "application/xml, */*")
            r = requests.get(url, headers=h, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r
            if not silencioso:
                print(f"      [{perfil}] HTTP {r.status_code}")
            # Un 404 es del recurso, no de la cabecera: cambiar de perfil
            # no lo va a arreglar y solo gasta tres viajes de red.
            if r.status_code == 404:
                return None
        except Exception as exc:
            if not silencioso:
                print(f"      [{perfil}] {type(exc).__name__}")
    return None


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def catalogo() -> list[tuple[str, str]]:
    r = pedir(f"{BASE}/dataflow/{AGENCIA}/all/latest")
    if r is None:
        return []
    try:
        raiz = ET.fromstring(r.content)
    except ET.ParseError:
        return []

    flujos = []
    for el in raiz.iter():
        if local(el.tag) != "Dataflow" or not el.get("id"):
            continue
        nombre = next((h.text.strip() for h in el.iter()
                       if local(h.tag) == "Name" and (h.text or "").strip()), "")
        flujos.append((el.get("id"), nombre))
    return sorted(flujos)


# --------------------------------------------------------------------------
Clave = tuple[tuple[str, str], ...]   # (dimensión, valor) ordenadas


def _parsear(contenido: bytes) -> dict[Clave, list[tuple[str, float]]]:
    """
    Devuelve las observaciones AGRUPADAS por serie.

    Agrupar no es un lujo: un dataflow puede contener varias series a la vez.
    El de IBR trae 25.580 observaciones para 5.844 días — son varios plazos
    mezclados. Aplanarlos todos en un mismo ticker haría que colisionaran en
    la clave (ticker, fecha) y el upsert guardaría uno al azar cada noche.
    """
    try:
        raiz = ET.fromstring(contenido)
    except ET.ParseError as exc:
        print(f"      respuesta no parseable: {exc}")
        return {}

    grupos: dict[Clave, list[tuple[str, float]]] = {}

    for serie in raiz.iter():
        if local(serie.tag) != "Series":
            continue

        clave: list[tuple[str, str]] = []
        for hijo in serie:
            if local(hijo.tag) != "SeriesKey":
                continue
            for v in hijo:
                if local(v.tag) == "Value" and v.get("id"):
                    clave.append((v.get("id"), v.get("value") or ""))
        # Formato structure-specific: las dimensiones son atributos.
        if not clave:
            clave = [(k, v) for k, v in serie.attrib.items()
                     if k not in ("TIME_PERIOD", "OBS_VALUE")]

        obs: list[tuple[str, float]] = []
        for el in serie.iter():
            if local(el.tag) != "Obs":
                continue
            periodo = valor = None
            for hijo in el:
                n = local(hijo.tag)
                if n == "ObsDimension":
                    periodo = hijo.get("value")
                elif n == "ObsValue":
                    valor = hijo.get("value")
            if periodo is None:
                periodo = el.get("TIME_PERIOD")
            if valor is None:
                valor = el.get("OBS_VALUE")
            if periodo and valor not in (None, "", "NaN"):
                try:
                    obs.append((periodo, float(valor)))
                except ValueError:
                    pass

        if obs:
            grupos.setdefault(tuple(sorted(clave)), []).extend(obs)

    return grupos


def descargar(flujo: str, desde: int) -> dict[Clave, list[tuple[str, float]]]:
    """
    Trae el flujo histórico y, si existe, el de últimos datos, y los fusiona.

    Banrep parte cada serie en dos: `_HIST` cierra en el último año completo
    y `_LATEST` cubre el actual. Pedir solo el histórico deja la serie
    congelada en diciembre pasado — que es exactamente lo que pasó con la TRM.
    """
    # El _HIST cierra en el último año completo y el _LATEST trae UN solo
    # dato: el de hoy. Entre ambos queda el año en curso sin cubrir. El
    # catálogo también publica flujos con el nombre base (DF_TRM, DF_IBR),
    # que son los candidatos naturales para rellenar ese tramo.
    base = flujo
    for sufijo in ("_DAILY_HIST", "_MONTHLY_HIST", "_HIST"):
        if base.upper().endswith(sufijo):
            base = base[: -len(sufijo)]
            break

    candidatos: list[str] = []
    for c in (flujo,
              flujo[:-5] + "_LATEST" if flujo.upper().endswith("_HIST") else None,
              base, f"{base}_HIST"):
        if c and c not in candidatos:
            candidatos.append(c)

    fusion: dict[Clave, dict[str, float]] = {}
    for f in candidatos:
        r, usada = pedir_datos(f, desde)
        if r is None:
            print(f"      {f}: sin respuesta con ninguna variante de URL")
            continue
        grupos = _parsear(r.content)
        n = sum(len(v) for v in grupos.values())
        print(f"      {f}: {n:,} observaciones en {len(grupos)} serie(s)   [{usada}]")
        # El LATEST se aplica después y pisa al HIST donde se solapen.
        for clave, obs in grupos.items():
            fusion.setdefault(clave, {}).update(dict(obs))

    return {k: sorted(v.items()) for k, v in fusion.items()}


def pedir_datos(flujo: str, desde: int) -> tuple[requests.Response | None, str]:
    """
    Prueba varias formas de pedir el mismo flujo hasta que una responda.

    Los flujos `_LATEST` diarios devuelven 404 con la URL que sí funciona para
    los `_HIST` y para el COLCAP mensual. No hay documentación que explique la
    diferencia, así que en vez de adivinar se prueban las variantes plausibles
    —sin filtro de fechas, sin versión fija, sin agencia— y se informa de cuál
    funcionó, para poder fijarla después.
    """
    cola = "dimensionAtObservation=TIME_PERIOD&detail=full"
    periodos = f"startPeriod={desde}&endPeriod={date.today().year}&"

    variantes = [
        ("v1.0 con fechas",  f"{AGENCIA},{flujo},1.0/all/ALL/?{periodos}{cola}"),
        ("v1.0 sin fechas",  f"{AGENCIA},{flujo},1.0/all/ALL/?{cola}"),
        ("vlatest",          f"{AGENCIA},{flujo},latest/all/ALL/?{cola}"),
        ("sin version",      f"{AGENCIA},{flujo}/all/ALL/?{cola}"),
        ("agencia all",      f"all,{flujo},latest/all/ALL/?{cola}"),
        ("solo flujo",       f"{flujo}/all/ALL/?{cola}"),
    ]

    for etiqueta, ruta in variantes:
        r = pedir(f"{BASE}/data/{ruta}", silencioso=True)
        if r is not None:
            return r, etiqueta
    return None, ""


def normalizar_fecha(periodo: str) -> str | None:
    """
    SDMX no usa un solo formato. Banrep devuelve 20100101 en las series
    diarias y 2010-01 en las mensuales, dentro del mismo servicio.

    El dato mensual se ancla al ÚLTIMO día del mes. Un nivel mensual describe
    el mes que terminó; ponerlo en el día 1 lo adelantaría un mes entero
    frente a las series diarias, y eso se leería después como si el COLCAP
    anticipara al S&P 500. Un desfase temporal produce señales espectaculares
    y completamente falsas.
    """
    p = periodo.strip()
    if len(p) == 10 and p[4] == "-":            # 2024-03-15
        return p
    if len(p) == 8 and p.isdigit():             # 20240315
        return f"{p[:4]}-{p[4:6]}-{p[6:]}"
    if len(p) == 7 and p[4] == "-":             # 2024-03
        anio, mes = int(p[:4]), int(p[5:7])
        return f"{p}-{monthrange(anio, mes)[1]:02d}"
    if len(p) == 6 and p.isdigit():             # 202403
        anio, mes = int(p[:4]), int(p[4:])
        return f"{p[:4]}-{p[4:]}-{monthrange(anio, mes)[1]:02d}"
    if len(p) == 4 and p.isdigit():             # 2024
        return f"{p}-12-31"
    return None


def elegir_serie(grupos: dict[Clave, list], filtro: dict | None
                 ) -> tuple[list | None, str]:
    """Aplica el filtro de dimensiones y exige que quede exactamente una."""
    if not grupos:
        return None, "sin series"

    candidatas = grupos
    if filtro:
        candidatas = {
            k: v for k, v in grupos.items()
            if all(any(d == dim and val == str(v2) for d, val in k)
                   for dim, v2 in filtro.items())
        }

    if len(candidatas) == 1:
        return next(iter(candidatas.values())), "ok"

    if not candidatas:
        return None, f"el filtro {filtro} no coincide con ninguna serie"

    # Varias series y sin filtro que las distinga: NO se elige al azar.
    print(f"      {len(candidatas)} series distintas en este flujo:")
    for k, v in sorted(candidatas.items(), key=lambda x: -len(x[1]))[:12]:
        dims = ", ".join(f"{d}={val}" for d, val in k) or "(sin dimensiones)"
        print(f"        {len(v):>6,} obs   {dims}")
    return None, "hay varias series; define activos.serie_filtro"


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Series del Banco de la República")
    ap.add_argument("--escribir", action="store_true")
    ap.add_argument("--desde", type=int, default=2010)
    ap.add_argument("--solo", help="un solo ticker, p. ej. TRM")
    ap.add_argument("--catalogo", action="store_true",
                    help="listar los dataflows disponibles y salir")
    args = ap.parse_args()

    if args.catalogo:
        flujos = catalogo()
        print(f"\n{len(flujos)} dataflows en {AGENCIA}:\n")
        for fid, nombre in flujos:
            marca = "  <-- diario" if "DAILY" in fid.upper() else ""
            print(f"  {fid:<40} {nombre[:40]}{marca}")
        return

    sb = conectar()
    series = (
        sb.table("activos")
        .select("ticker,nombre,frecuencia,serie_externa,serie_filtro")
        .eq("fuente_datos", "banrep_sdmx")
        .eq("activo", True)
        .not_.is_("serie_externa", "null")
        .execute()
        .data
    )
    if args.solo:
        series = [s for s in series if s["ticker"] == args.solo.upper()]
    if not series:
        sys.exit("No hay series de banrep_sdmx que ingestar.")

    print(f"\n{'='*70}\nBANCO DE LA REPÚBLICA — {len(series)} series\n{'='*70}")
    total = 0

    for s in series:
        t, flujo, frec = s["ticker"], s["serie_externa"], s["frecuencia"]
        print(f"\n  {t}  ({frec})  {flujo}")

        grupos = descargar(flujo, args.desde)
        obs, motivo = elegir_serie(grupos, s.get("serie_filtro"))

        if obs is None:
            print(f"      SIN ESCRIBIR: {motivo}")
            if args.escribir:
                sb.table("activos").update({"verificado": False}).eq("ticker", t).execute()
                log(sb, "ingestar_banrep", t, False, error=f"{flujo}: {motivo}")
            continue

        print(f"      serie elegida: {len(obs):,} obs   "
              f"{obs[0][0]} a {obs[-1][0]}   último={obs[-1][1]:,.2f}")

        filas, sin_fecha = [], 0
        for periodo, valor in obs:
            f = normalizar_fecha(periodo)
            if f is None:
                sin_fecha += 1
                continue
            # Índices, tasas y tipos de cambio no tienen OHLC ni volumen:
            # solo un nivel publicado. El esquema lo permite porque solo
            # `cierre` es obligatorio.
            filas.append({"ticker": t, "fecha": f, "cierre": round(valor, 4)})

        if sin_fecha:
            print(f"      AVISO: {sin_fecha} periodos con formato no reconocido "
                  f"(ejemplo: {obs[0][0]!r})")

        # Huecos. Un salto de ocho meses seguido de un punto suelto no es una
        # serie al día: es una serie vieja con un dato flotando al final, y la
        # resta entre ambos extremos produce un retorno inventado enorme.
        limite = 40 if frec == "mensual" else 10
        fechas = sorted(date.fromisoformat(f["fecha"]) for f in filas)
        huecos = [(a, b, (b - a).days) for a, b in zip(fechas, fechas[1:])
                  if (b - a).days > limite]
        if huecos:
            peor = max(huecos, key=lambda h: h[2])
            print(f"      AVISO: {len(huecos)} hueco(s) mayores de {limite} días. "
                  f"El mayor: {peor[0]} → {peor[1]} ({peor[2]} días)")
            print(f"      Ningún retorno ni correlación debe cruzar ese tramo. "
                  f"Consulta v_huecos_series.")
        if not filas:
            print("      SIN ESCRIBIR: ninguna fecha utilizable")
            continue
        if not args.escribir:
            print(f"      {len(filas):,} filas listas (añade --escribir)")
            continue

        try:
            n = upsert(sb, "precios_diarios", filas, "ticker,fecha")
            sb.table("activos").update({"verificado": True}).eq("ticker", t).execute()
            log(sb, "ingestar_banrep", t, True, filas=n)
            print(f"      {n:,} filas escritas")
            total += n
        except Exception as exc:
            print(f"      ERROR al escribir: {exc}")
            log(sb, "ingestar_banrep", t, False, error=str(exc))

    if args.escribir:
        print(f"\n  Total: {total:,} filas.")
    else:
        print("\n  Exploración terminada. Añade --escribir para guardarlo.")


if __name__ == "__main__":
    main()
