"""
ingestar_precios.py — Fase 2.

Descarga el histórico diario de los activos que vienen de Yahoo y lo escribe
en `precios_diarios`. Después clasifica cada día en `regimenes_mercado`.

Diseñado para correr a diario sin supervisión:
  - Escritura idempotente: volver a ejecutarlo no duplica nada.
  - Descarga en lote, nunca ticker por ticker (evita el 429 de Yahoo).
  - Reintento en serie de lo que falle en lote, antes de darlo por muerto.
  - Todo queda registrado en `ingesta_log`, exitoso o no.

Uso:
    python scripts/ingestar_precios.py                # incremental (60 días)
    python scripts/ingestar_precios.py --historico    # carga inicial (10 años)
    python scripts/ingestar_precios.py --solo-regimen
"""

from __future__ import annotations

import argparse
import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
import yfinance as yf
from comun import conectar, leer_todo, log, upsert

CACHE = Path.home() / ".cache" / "yfinance"
CACHE.mkdir(parents=True, exist_ok=True)
try:
    yf.set_tz_cache_location(str(CACHE))
except Exception:
    pass

# Umbrales del régimen de mercado.
#
# Son convencionales, no sagrados: VIX bajo 20 es la calma habitual, 20-30 es
# nerviosismo, y por encima de 30 el mercado ya no se comporta como en los
# libros. Están aquí arriba y no enterrados en el código precisamente para
# que puedas discutirlos con los datos en la mano más adelante.
VIX_ESTRES = 20.0
VIX_SHOCK = 30.0
VENTANA_VOL = 20  # sesiones para la volatilidad realizada


# --------------------------------------------------------------------------
def descargar(simbolos: list[str], periodo: str) -> tuple[dict, list[str]]:
    """Descarga en lote y devuelve (datos por ticker, los que fallaron)."""
    crudo = yf.download(simbolos, period=periodo, interval="1d",
                        group_by="ticker", auto_adjust=False,
                        progress=False, threads=True)

    series, fallidos = {}, []
    for t in simbolos:
        try:
            df = crudo[t] if len(simbolos) > 1 else crudo
            df = df.dropna(subset=["Close"])
            if len(df):
                series[t] = df
            else:
                fallidos.append(t)
        except Exception:
            fallidos.append(t)

    # Segunda pasada en serie. Un fallo en lote puede ser un timeout o una
    # colisión de caché; condenar el ticker ahí produce huecos silenciosos
    # en el histórico, que es el peor error posible en una serie temporal.
    if fallidos:
        print(f"  Reintentando en serie: {', '.join(fallidos)}")
        aun_fallan = []
        for t in fallidos:
            try:
                df = yf.download(t, period=periodo, interval="1d",
                                 auto_adjust=False, progress=False,
                                 threads=False).dropna(subset=["Close"])
                if len(df):
                    series[t] = df
                else:
                    aun_fallan.append(t)
            except Exception:
                aun_fallan.append(t)
            time.sleep(1.0)
        fallidos = aun_fallan

    return series, fallidos


def a_filas(ticker: str, df: pd.DataFrame) -> list[dict]:
    """Convierte el DataFrame de yfinance en filas para `precios_diarios`."""
    filas = []
    for idx, r in df.iterrows():
        cierre = r.get("Close")
        if pd.isna(cierre):
            continue

        def num(v):
            return None if pd.isna(v) else round(float(v), 4)

        vol = r.get("Volume")
        filas.append({
            "ticker": ticker,
            "fecha": idx.date().isoformat(),
            "apertura": num(r.get("Open")),
            "maximo": num(r.get("High")),
            "minimo": num(r.get("Low")),
            "cierre": round(float(cierre), 4),
            "volumen": None if pd.isna(vol) else int(vol),
        })
    return filas


# --------------------------------------------------------------------------
def ingestar(sb, periodo: str) -> None:
    activos = (
        sb.table("activos")
        .select("ticker,nombre")
        .eq("activo", True)
        .eq("fuente_datos", "yfinance")
        .execute()
        .data
    )
    simbolos = [a["ticker"] for a in activos]
    nombres = {a["ticker"]: a["nombre"] for a in activos}

    print(f"\n{'='*70}\nINGESTA DE PRECIOS — {len(simbolos)} activos, ventana {periodo}\n{'='*70}")

    series, fallidos = descargar(simbolos, periodo)
    total = 0

    for t in simbolos:
        if t not in series:
            continue
        filas = a_filas(t, series[t])
        if not filas:
            fallidos.append(t)
            continue
        try:
            n = upsert(sb, "precios_diarios", filas, "ticker,fecha")
            rango = f"{filas[0]['fecha']} a {filas[-1]['fecha']}"
            print(f"  {t:<12} {nombres[t][:28]:<30} {n:>5} filas   {rango}")
            sb.table("activos").update({"verificado": True}).eq("ticker", t).execute()
            log(sb, "ingestar_precios", t, True, filas=n)
            total += n
        except Exception as exc:
            print(f"  {t:<12} ERROR al escribir: {exc}")
            log(sb, "ingestar_precios", t, False, error=str(exc))
            fallidos.append(t)

    for t in fallidos:
        sb.table("activos").update({"verificado": False}).eq("ticker", t).execute()
        log(sb, "ingestar_precios", t, False, error="sin datos tras lote y reintento")

    print(f"\n  {total:,} filas escritas. {len(fallidos)} activos sin datos.")
    if fallidos:
        print(f"  Sin datos: {', '.join(fallidos)}")


# --------------------------------------------------------------------------
def calcular_regimen(sb) -> None:
    """
    Clasifica cada día como normal, estrés o shock.

    Esto es lo que da sentido al modelo de tendencia: no pretende predecir
    guerras ni colapsos, pretende predecir la deriva cuando no los hay. Sin
    esta tabla esa afirmación no se puede comprobar; con ella, cada predicción
    queda etiquetada con las condiciones en que se emitió y en que se resolvió.
    """
    print(f"\n{'='*70}\nCLASIFICACIÓN DE RÉGIMEN\n{'='*70}")

    def serie(ticker: str) -> pd.Series:
        # Paginado, no .limit(): PostgREST devuelve 1.000 filas y calla.
        filas = leer_todo(sb, "precios_diarios", "fecha,cierre",
                          filtros={"ticker": ticker}, orden="fecha")
        if not filas:
            return pd.Series(dtype=float)
        s = pd.Series(
            [float(f["cierre"]) for f in filas],
            index=pd.to_datetime([f["fecha"] for f in filas]),
        )
        return s[~s.index.duplicated(keep="last")]

    vix = serie("^VIX")
    spx = serie("^GSPC")

    if vix.empty:
        print("  Sin datos del VIX. Ejecuta primero la ingesta de precios.")
        return

    # Volatilidad realizada del S&P 500: desviación de los retornos diarios
    # en 20 sesiones, anualizada. Sirve de contraste — el VIX es expectativa,
    # esto es lo que de verdad pasó.
    vol = pd.Series(dtype=float)
    if not spx.empty:
        vol = np.log(spx / spx.shift(1)).rolling(VENTANA_VOL).std() * np.sqrt(252)

    filas = []
    for fecha, v in vix.items():
        if pd.isna(v):
            continue
        if v >= VIX_SHOCK:
            estado, nota = "shock", f"VIX {v:.1f} >= {VIX_SHOCK}"
        elif v >= VIX_ESTRES:
            estado, nota = "estres", f"VIX {v:.1f} >= {VIX_ESTRES}"
        else:
            estado, nota = "normal", f"VIX {v:.1f}"

        vr = vol.get(fecha, np.nan) if len(vol) else np.nan
        filas.append({
            "fecha": fecha.date().isoformat(),
            "vix_cierre": round(float(v), 4),
            "vol_realizada": None if pd.isna(vr) else round(float(vr), 5),
            "estado": estado,
            "nota": nota,
        })

    n = upsert(sb, "regimenes_mercado", filas, "fecha")
    log(sb, "calcular_regimen", None, True, filas=n)

    resumen = pd.Series([f["estado"] for f in filas]).value_counts()
    print(f"  {n:,} días clasificados:")
    for estado in ("normal", "estres", "shock"):
        c = int(resumen.get(estado, 0))
        pct = 100 * c / len(filas) if filas else 0
        print(f"    {estado:<8} {c:>6,} días  ({pct:4.1f}%)")
    print(f"\n  Umbrales actuales: estrés >= VIX {VIX_ESTRES}, shock >= VIX {VIX_SHOCK}")


# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Ingesta de precios y régimen")
    p.add_argument("--historico", action="store_true",
                   help="carga inicial de 10 años en vez de la ventana corta")
    p.add_argument("--solo-regimen", action="store_true")
    p.add_argument("--solo-precios", action="store_true")
    args = p.parse_args()

    sb = conectar()

    if not args.solo_regimen:
        ingestar(sb, "10y" if args.historico else "60d")
    if not args.solo_precios:
        calcular_regimen(sb)

    print(f"\n{'='*70}")
    print("Comprueba el resultado con:")
    print("  select * from v_salud_ingesta order by dias_de_retraso desc nulls first;")
    print("  select estado, count(*) from regimenes_mercado group by estado;")


if __name__ == "__main__":
    main()
