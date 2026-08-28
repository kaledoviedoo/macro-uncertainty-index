"""
ingestar_datos_abiertos.py — Series desde datos.gov.co (Socrata).

El SDMX del Banco de la República no publica el año en curso en frecuencia
diaria: el flujo `_HIST` cierra en diciembre del año anterior y el `_LATEST`
trae una sola observación, la de hoy. Entre ambos queda un hueco de meses.

El portal de datos abiertos sí tiene las series completas y al día:

    TRM   32sa-8pi3   8.331 filas desde 1991-12-02
    IBR   ev8i-uzwt   (esquema por explorar)

Uso:
    python scripts/ingestar_datos_abiertos.py             # explora, no escribe
    python scripts/ingestar_datos_abiertos.py --escribir
    python scripts/ingestar_datos_abiertos.py --solo TRM --escribir
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from comun import CONTACTO, conectar, log, upsert

BASE = "https://www.datos.gov.co/resource"
PAGINA = 50_000          # el máximo que acepta Socrata por petición
CABECERAS = {
    "User-Agent": f"MotorCausal/0.1 (+{CONTACTO})",
    "Accept": "application/json",
}


def pedir(recurso: str, params: dict) -> list[dict] | None:
    try:
        r = requests.get(f"{BASE}/{recurso}.json", params=params,
                         headers=CABECERAS, timeout=90)
        if r.status_code != 200:
            print(f"      HTTP {r.status_code}: {r.text[:160]}")
            return None
        return r.json()
    except Exception as exc:
        print(f"      {type(exc).__name__}: {exc}")
        return None


def explorar(recurso: str) -> list[str]:
    """Devuelve los nombres de campo, para no adivinar el esquema."""
    muestra = pedir(recurso, {"$limit": 1})
    if not muestra:
        return []
    campos = list(muestra[0].keys())
    print(f"      campos: {', '.join(campos)}")
    print(f"      ejemplo: {muestra[0]}")
    return campos


def adivinar_campos(campos: list[str]) -> tuple[str | None, str | None]:
    """
    Heurística para el caso en que no haya filtro configurado.

    Solo sirve para explorar: si acierta se propone la configuración, pero
    no se escribe nada con ella sin que quede fijada en la base. Un mapeo
    adivinado que funciona hoy y cambia mañana es peor que ninguno.
    """
    fecha = next((c for c in campos
                  if any(k in c.lower() for k in ("vigenciadesde", "fecha", "date"))), None)
    valor = next((c for c in campos
                  if any(k in c.lower() for k in ("valor", "tasa", "value"))), None)
    return fecha, valor


def descargar(recurso: str, campo_fecha: str, campo_valor: str,
              where: str | None) -> list[tuple[str, float]]:
    """Pagina el dataset entero. Socrata devuelve 1.000 filas si no se pide más."""
    filas: list[tuple[str, float]] = []
    offset = 0
    while True:
        params = {
            "$select": f"{campo_fecha},{campo_valor}",
            "$order": f"{campo_fecha} ASC",
            "$limit": PAGINA,
            "$offset": offset,
        }
        if where:
            params["$where"] = where

        trozo = pedir(recurso, params)
        if trozo is None:
            break
        for f in trozo:
            crudo, val = f.get(campo_fecha), f.get(campo_valor)
            if crudo in (None, "") or val in (None, ""):
                continue
            try:
                # Socrata da 2026-08-22T00:00:00.000
                fecha = datetime.fromisoformat(str(crudo).replace("Z", "")).date()
                filas.append((fecha.isoformat(), float(val)))
            except (ValueError, TypeError):
                continue
        if len(trozo) < PAGINA:
            break
        offset += PAGINA

    # Un mismo día puede aparecer repetido; se queda el último valor.
    return sorted(dict(filas).items())


# --------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Series de datos.gov.co")
    ap.add_argument("--escribir", action="store_true")
    ap.add_argument("--solo")
    args = ap.parse_args()

    sb = conectar()
    series = (
        sb.table("activos")
        .select("ticker,nombre,frecuencia,serie_externa,serie_filtro")
        .eq("fuente_datos", "datos_gov")
        .eq("activo", True)
        .not_.is_("serie_externa", "null")
        .execute()
        .data
    )
    if args.solo:
        series = [s for s in series if s["ticker"] == args.solo.upper()]
    if not series:
        sys.exit("No hay series de datos_gov que ingestar.")

    print(f"\n{'='*70}\nDATOS ABIERTOS DE COLOMBIA — {len(series)} series\n{'='*70}")
    total = 0

    for s in series:
        t, recurso = s["ticker"], s["serie_externa"]
        filtro = s.get("serie_filtro") or {}
        print(f"\n  {t}   dataset {recurso}")

        campo_fecha, campo_valor = filtro.get("fecha"), filtro.get("valor")

        if not (campo_fecha and campo_valor):
            campos = explorar(recurso)
            if not campos:
                continue
            sug_f, sug_v = adivinar_campos(campos)
            print(f"\n      Sin mapeo configurado. Sugerencia a partir del esquema:")
            print(f'      update activos set serie_filtro = '
                  f'\'{{"fecha":"{sug_f}","valor":"{sug_v}"}}\'::jsonb '
                  f"where ticker = '{t}';")
            print("      Revisa que esos campos sean los correctos antes de fijarlo.")
            continue

        obs = descargar(recurso, campo_fecha, campo_valor, filtro.get("where"))
        if not obs:
            print("      sin observaciones")
            if args.escribir:
                log(sb, "ingestar_datos_abiertos", t, False, error=f"{recurso}: vacío")
            continue

        print(f"      {len(obs):,} observaciones   {obs[0][0]} a {obs[-1][0]}"
              f"   último={obs[-1][1]:,.2f}")

        # Huecos: el motivo por el que cambiamos de fuente. Si la nueva
        # también los tiene, hay que saberlo antes de escribir.
        limite = 45 if s["frecuencia"] == "mensual" else 15
        fechas = [date.fromisoformat(f) for f, _ in obs]
        huecos = [(a, b, (b - a).days) for a, b in zip(fechas, fechas[1:])
                  if (b - a).days > limite]
        if huecos:
            peor = max(huecos, key=lambda h: h[2])
            print(f"      AVISO: {len(huecos)} hueco(s) > {limite} días. "
                  f"El mayor: {peor[0]} → {peor[1]} ({peor[2]} días)")
        else:
            print(f"      Sin huecos mayores de {limite} días.")

        if not args.escribir:
            print("      (añade --escribir para guardar)")
            continue

        filas = [{"ticker": t, "fecha": f, "cierre": round(v, 4)} for f, v in obs]
        try:
            n = upsert(sb, "precios_diarios", filas, "ticker,fecha")
            sb.table("activos").update({"verificado": True}).eq("ticker", t).execute()
            log(sb, "ingestar_datos_abiertos", t, True, filas=n)
            print(f"      {n:,} filas escritas")
            total += n
        except Exception as exc:
            print(f"      ERROR al escribir: {exc}")
            log(sb, "ingestar_datos_abiertos", t, False, error=str(exc))

    if args.escribir:
        print(f"\n  Total: {total:,} filas.")


if __name__ == "__main__":
    main()
