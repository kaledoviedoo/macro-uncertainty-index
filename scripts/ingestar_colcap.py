"""
ingestar_colcap.py — El COLCAP desde su fuente primaria.

Yahoo no tiene el COLCAP y el ETF proxy (GXG) tampoco devuelve serie. Pero
el Banco de la República publica el índice por un servicio web SDMX oficial.
Eso es mejor que cualquier proxy: es el dato del banco central, no la lectura
de un intermediario.

    Endpoint:  https://totoro.banrep.gov.co/nsi-jax-ws/rest
    Formato:   SDMX-ML 2.1 (XML)
    Flujos:    DF_COLCAP_*  (el catálogo dirá cuáles existen de verdad)

El script primero PREGUNTA qué flujos hay en vez de asumirlo. La
documentación técnica solo menciona los mensuales, pero el portal describe
la serie como diaria; en lugar de adivinar cuál es cierto, se consulta el
catálogo y se prefiere el diario si aparece.

Uso:
    python scripts/ingestar_colcap.py              # explora y muestra
    python scripts/ingestar_colcap.py --escribir   # además guarda en la BD
    python scripts/ingestar_colcap.py --escribir --desde 2015
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
TICKER = "COLCAP"
LISTAR_TODO = False


def pedir(url: str, timeout: int = 60) -> requests.Response | None:
    """Prueba los perfiles de cabecera hasta que uno devuelva 200."""
    for perfil, cabeceras in PERFILES_UA.items():
        try:
            h = dict(cabeceras)
            h["Accept"] = "application/vnd.sdmx.genericdata+xml;version=2.1, application/xml, */*"
            r = requests.get(url, headers=h, timeout=timeout)
            if r.status_code == 200 and r.content:
                return r
            print(f"    [{perfil}] HTTP {r.status_code}")
        except Exception as exc:
            print(f"    [{perfil}] {type(exc).__name__}: {exc}")
    return None


def local(tag: str) -> str:
    """Nombre de etiqueta sin el namespace, que varía entre versiones SDMX."""
    return tag.rsplit("}", 1)[-1]


# --------------------------------------------------------------------------
def descubrir_flujos() -> list[tuple[str, str]]:
    """Pregunta al catálogo qué dataflows existen y filtra los del COLCAP."""
    print(f"\n{'='*70}\nCATÁLOGO DE FLUJOS SDMX\n{'='*70}")
    r = pedir(f"{BASE}/dataflow/{AGENCIA}/all/latest")
    if r is None:
        print("  No se pudo leer el catálogo.")
        return []

    try:
        raiz = ET.fromstring(r.content)
    except ET.ParseError as exc:
        print(f"  El catálogo no es XML válido: {exc}")
        print(f"  Primeros 300 bytes: {r.content[:300]!r}")
        return []

    todos: list[tuple[str, str]] = []
    for el in raiz.iter():
        if local(el.tag) != "Dataflow":
            continue
        fid = el.get("id")
        if not fid:
            continue
        nombre = ""
        for hijo in el.iter():
            if local(hijo.tag) == "Name" and (hijo.text or "").strip():
                nombre = hijo.text.strip()
                break
        todos.append((fid, nombre))

    colcap = [(f, n) for f, n in todos if "COLCAP" in f.upper()]
    print(f"  {len(todos)} flujos en total, {len(colcap)} con COLCAP en el id:")
    for fid, nombre in colcap:
        print(f"    {fid:<38} {nombre[:40]}")

    if LISTAR_TODO:
        print(f"\n  Catálogo completo ({len(todos)} flujos):")
        for fid, nombre in sorted(todos):
            marca = "  <-- diario" if "DAILY" in fid.upper() else ""
            print(f"    {fid:<40} {nombre[:38]}{marca}")

    return colcap


def elegir(flujos: list[tuple[str, str]]) -> str | None:
    """Diario antes que mensual; histórico antes que 'latest'."""
    if not flujos:
        return None
    ids = [f for f, _ in flujos]

    def buscar(*claves: str) -> str | None:
        for i in ids:
            if all(k in i.upper() for k in claves):
                return i
        return None

    return (buscar("DAILY", "HIST") or buscar("DAILY")
            or buscar("MONTHLY", "HIST") or buscar("MONTHLY") or ids[0])


# --------------------------------------------------------------------------
def descargar_serie(flujo: str, desde: int) -> list[tuple[str, float]]:
    """Devuelve [(fecha ISO, valor)] del flujo indicado."""
    url = (f"{BASE}/data/{AGENCIA},{flujo},1.0/all/ALL/"
           f"?startPeriod={desde}&endPeriod={date.today().year}"
           f"&dimensionAtObservation=TIME_PERIOD&detail=full")
    print(f"\n{'='*70}\nDESCARGANDO {flujo}\n{'='*70}")
    print(f"  {url}")

    r = pedir(url)
    if r is None:
        return []

    try:
        raiz = ET.fromstring(r.content)
    except ET.ParseError as exc:
        print(f"  Respuesta no parseable: {exc}")
        print(f"  Primeros 300 bytes: {r.content[:300]!r}")
        return []

    obs: list[tuple[str, float]] = []
    for el in raiz.iter():
        if local(el.tag) != "Obs":
            continue
        periodo = valor = None
        # Formato genérico: hijos ObsDimension / ObsValue con atributo value.
        for hijo in el:
            nombre = local(hijo.tag)
            if nombre == "ObsDimension":
                periodo = hijo.get("value")
            elif nombre == "ObsValue":
                valor = hijo.get("value")
        # Formato structure-specific: todo en atributos del propio Obs.
        if periodo is None:
            periodo = el.get("TIME_PERIOD")
        if valor is None:
            valor = el.get("OBS_VALUE")

        if periodo and valor not in (None, "", "NaN"):
            try:
                obs.append((periodo, float(valor)))
            except ValueError:
                continue

    obs.sort()
    print(f"  {len(obs)} observaciones")
    if obs:
        print(f"  Rango: {obs[0][0]} a {obs[-1][0]}")
        print(f"  Último valor: {obs[-1][1]:,.2f}")
        print(f"  Muestra: {obs[:3]}")
    return obs


def normalizar_fecha(periodo: str) -> str | None:
    """
    SDMX da 2024-03-15, 2024-03 o 2024. Hay que llevarlo a un día concreto.

    Para el dato mensual se ancla al ÚLTIMO día del mes, no al primero.
    Un nivel de índice mensual describe el mes que acaba de terminar; ponerlo
    en el día 1 lo adelantaría un mes entero frente a las series diarias, y
    ese desfase se leería después como si el COLCAP anticipara al S&P 500.
    """
    p = periodo.strip()
    if len(p) == 10:
        return p
    if len(p) == 7:
        anio, mes = int(p[:4]), int(p[5:7])
        ultimo = monthrange(anio, mes)[1]
        return f"{p}-{ultimo:02d}"
    if len(p) == 4:
        return f"{p}-12-31"
    return None


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="COLCAP desde el Banco de la República")
    ap.add_argument("--escribir", action="store_true", help="guardar en precios_diarios")
    ap.add_argument("--desde", type=int, default=2010)
    ap.add_argument("--flujo", help="forzar un dataflow concreto")
    ap.add_argument("--listar", action="store_true",
                    help="imprimir el catálogo completo de flujos del Banrep")
    args = ap.parse_args()

    global LISTAR_TODO
    LISTAR_TODO = args.listar

    flujo = args.flujo
    if not flujo:
        flujo = elegir(descubrir_flujos())
    if not flujo:
        sys.exit("\nNo se identificó ningún flujo del COLCAP. Pásalo con --flujo.")

    print(f"\n  Flujo elegido: {flujo}")
    obs = descargar_serie(flujo, args.desde)
    if not obs:
        sys.exit("\nSin observaciones. Prueba otro flujo con --flujo.")

    if not args.escribir:
        print("\n  Exploración terminada. Añade --escribir para guardarlo.")
        return

    sb = conectar()
    filas = []
    for periodo, valor in obs:
        f = normalizar_fecha(periodo)
        if f:
            # El COLCAP es un índice: solo hay nivel de cierre, no OHLC ni volumen.
            filas.append({"ticker": TICKER, "fecha": f, "cierre": round(valor, 4)})

    n = upsert(sb, "precios_diarios", filas, "ticker,fecha")
    sb.table("activos").update({"verificado": True}).eq("ticker", TICKER).execute()
    log(sb, "ingestar_colcap", TICKER, True, filas=n)
    print(f"\n  {n:,} filas escritas en precios_diarios para {TICKER}.")
    print(f"  Flujo usado: {flujo} — anótalo, es el que usará la ingesta diaria.")


if __name__ == "__main__":
    main()
