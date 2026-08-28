"""
evaluar.py — El marcador de caídas.

La métrica que NO usamos es la exactitud. En el S&P 500 solo el 3,50 % de
los días caen más del 2 %, así que un modelo que jamás anuncie una caída
acierta el 96,50 %. Perseguir un 88 % de exactitud sería construir algo
peor que el silencio, y encima con apariencia de funcionar.

Las tres que sí valen:

  ELEVACIÓN   De los días que marcas, ¿qué fracción termina en caída,
              comparado con la frecuencia base? Elevación 3x significa que
              tus avisos triplican la probabilidad de acertar respecto a
              tirar una moneda cargada. Es la métrica principal.

  COBERTURA   De todas las caídas que hubo, ¿cuántas anticipaste? Sin
              esto, un modelo que marca dos días al año con elevación
              altísima parecería excelente y sería inútil.

  CALIBRACIÓN Cuando dices "18 %", ¿ocurre el 18 % de las veces? Es lo
              que permite usar el número para decidir algo.

Este script establece la LÍNEA BASE con señales que ya tienes —régimen,
VIX, drawdown, volatilidad reciente, calendario— antes de que el LLM entre
en escena. Lo que el LLM produzca en la fase 5 tendrá que superar esto, y
sin este número no habría con qué compararlo.

    python scripts/evaluar.py
    python scripts/evaluar.py --ticker ^GSPC --caida 2 --dias 5
    python scripts/evaluar.py --todos
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from comun import conectar, leer_todo


# ---------------------------------------------------------------------------
def cargar(sb, ticker: str) -> tuple[pd.Series, pd.DataFrame, set]:
    filas = leer_todo(sb, "precios_diarios", "fecha,cierre",
                      filtros={"ticker": ticker}, orden="fecha")
    if not filas:
        sys.exit(f"Sin precios para {ticker}")
    s = pd.Series([float(f["cierre"]) for f in filas],
                  index=pd.to_datetime([f["fecha"] for f in filas]))
    s = s[~s.index.duplicated(keep="last")].sort_index()

    reg = pd.DataFrame(leer_todo(sb, "regimenes_mercado", "fecha,estado,vix_cierre",
                                 orden="fecha"))
    if not reg.empty:
        reg["fecha"] = pd.to_datetime(reg["fecha"])
        reg = reg.set_index("fecha")

    ev = sb.table("eventos_calendario").select("fecha").execute().data
    return s, reg, {pd.Timestamp(e["fecha"]) for e in ev}


def objetivo(s: pd.Series, dias: int, caida_pct: float) -> pd.Series:
    """
    ¿Cae más de `caida_pct` en algún momento de los próximos `dias`?

    Devuelve 1.0, 0.0 o NaN. El NaN importa: los últimos días de la serie
    no tienen ventana futura completa, y darlos por "no cayó" los cuenta
    como aciertos del silencio. Es el mismo error de siempre —tratar lo
    desconocido como negativo— solo que al final de la muestra.

    Excepción: si la caída YA ocurrió dentro del tramo disponible, es un 1
    aunque la ventana esté incompleta. Eso sí se sabe.

    Mira el MÍNIMO de la ventana, no el cierre final: una caída del 6 % que
    se recupera antes de que termine el mes sigue siendo una caída que te
    habría dolido si estabas dentro.
    """
    fut = pd.concat([s.shift(-i) for i in range(1, dias + 1)], axis=1).min(axis=1)
    cayo = (fut / s - 1) < -(caida_pct / 100)
    completo = s.shift(-dias).notna()

    out = cayo.astype(float)
    out[~completo & ~cayo] = np.nan
    return out


def señales(s: pd.Series, reg: pd.DataFrame, fechas_ev: set) -> dict[str, pd.Series]:
    """
    Señales candidatas. TODAS usan solo información disponible el día t.

    El sesgo de anticipación es el error que hace que un backtest brille y
    la realidad no: basta un `rolling` sin desplazar para que la señal de
    hoy contenga el precio de mañana. Aquí cada media móvil termina en t.
    """
    v = s.where(s > 0)
    r = np.log(v / v.shift(1)).replace([np.inf, -np.inf], np.nan)

    vol5 = r.rolling(5).std()
    vol60 = r.rolling(60).std()
    max60 = s.rolling(60).max()
    caida_desde_max = s / max60 - 1

    estado = reg["estado"].reindex(s.index) if not reg.empty else pd.Series(index=s.index, dtype=object)
    vix = pd.to_numeric(reg["vix_cierre"], errors="coerce").reindex(s.index) \
        if not reg.empty else pd.Series(index=s.index, dtype=float)
    vix_p80 = vix.rolling(252).quantile(0.80)

    # ¿Hay evento de calendario en los próximos 5 días? Es información
    # conocida hoy: las fechas están publicadas con meses de antelación.
    prox_ev = pd.Series(False, index=s.index)
    if fechas_ev:
        for i, d in enumerate(s.index):
            prox_ev.iloc[i] = any(d < f <= d + pd.Timedelta(days=7) for f in fechas_ev)

    return {
        "régimen estrés":        estado.eq("estres"),
        "régimen shock":         estado.eq("shock"),
        "VIX sobre su p80 anual": vix > vix_p80,
        "VIX subió >10 % ayer":  (vix / vix.shift(1) - 1) > 0.10,
        "vol 5d > 1,5x vol 60d": vol5 > 1.5 * vol60,
        "caída >3 % desde máx 60d": caida_desde_max < -0.03,
        "evento en 7 días":      prox_ev,
        "estrés Y vol alta":     estado.eq("estres") & (vol5 > 1.5 * vol60),
    }


def z_dos_proporciones(x1, n1, x2, n2) -> float:
    """p a dos colas de que dos tasas sean iguales."""
    if n1 == 0 or n2 == 0:
        return 1.0
    p1, p2 = x1 / n1, x2 / n2
    p = (x1 + x2) / (n1 + n2)
    den = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if den == 0:
        return 1.0
    return math.erfc(abs((p1 - p2) / den) / math.sqrt(2))


# ---------------------------------------------------------------------------
def marcador(ticker: str, s: pd.Series, reg, fechas_ev, dias: int, caida: float):
    tgt = objetivo(s, dias, caida)
    sen = señales(s, reg, fechas_ev)

    tgt = tgt[tgt.notna()].astype(bool)
    base_n = int(tgt.sum())
    base_total = int(len(tgt))
    base = base_n / base_total if base_total else 0

    print(f"\n{'='*78}")
    print(f"{ticker}   ·   caída > {caida:.0f} % en {dias} días hábiles")
    print(f"{'='*78}")
    print(f"  Días analizados         {base_total:,}")
    print(f"  Días con caída          {base_n:,}")
    print(f"  FRECUENCIA BASE         {base*100:.2f} %")
    print(f"  Exactitud del modelo mudo (nunca avisa):  {(1-base)*100:.2f} %")
    print(f"  → cualquier objetivo de 'exactitud' por debajo de eso es un retroceso.\n")

    print(f"  {'SEÑAL':<26}{'MARCA':>7}{'ACIERTA':>9}{'ELEVAC':>8}"
          f"{'COBERT':>8}{'p':>9}")
    print(f"  {'-'*67}")

    filas = []
    for nombre, sig in sen.items():
        sig = sig.reindex(tgt.index).fillna(False).astype(bool)
        n_marca = int(sig.sum())
        if n_marca < 20:
            continue
        aciertos = int((sig & tgt).sum())
        precision = aciertos / n_marca
        cobertura = aciertos / base_n if base_n else 0
        elevacion = precision / base if base else 0
        p = z_dos_proporciones(aciertos, n_marca,
                               base_n - aciertos, base_total - n_marca)
        filas.append((elevacion, nombre, n_marca, precision, cobertura, p, base_total))

    for elev, nombre, n, prec, cob, p, tot in sorted(filas, reverse=True):
        marca = "  <<<" if elev >= 2 and p < 0.05 and cob >= 0.15 else ""
        print(f"  {nombre:<26}{n/tot*100:>6.0f}%{prec*100:>8.1f}%"
              f"{elev:>7.2f}x{cob*100:>7.0f}%{p:>9.4f}{marca}")

    print(f"\n  Marca  = qué % de los días levanta la mano")
    print(f"  Acierta = de esos días, cuántos terminaron en caída")
    print(f"  Elevac  = cuántas veces la frecuencia base ({base*100:.2f} %)")
    print(f"  Cobert  = qué % de todas las caídas capturó")
    print(f"  <<<     = supera el listón: elevación >= 2x, p < 0,05, cobertura >= 15 %")
    return filas


def main() -> None:
    ap = argparse.ArgumentParser(description="Marcador de caídas")
    ap.add_argument("--ticker", default="^GSPC")
    ap.add_argument("--caida", type=float, default=2.0, help="umbral en %%")
    ap.add_argument("--dias", type=int, default=5)
    ap.add_argument("--todos", action="store_true",
                    help="barrer varios umbrales y horizontes")
    a = ap.parse_args()

    sb = conectar(silencioso=True)
    s, reg, ev = cargar(sb, a.ticker)
    print(f"\n{a.ticker}: {len(s):,} precios de {s.index.min():%Y-%m-%d} "
          f"a {s.index.max():%Y-%m-%d}")
    print(f"{len(ev):,} fechas de evento en el calendario")

    if a.todos:
        for caida, dias in ((2, 5), (3, 5), (2, 20), (5, 20)):
            marcador(a.ticker, s, reg, ev, dias, caida)
    else:
        marcador(a.ticker, s, reg, ev, a.dias, a.caida)

    print(f"\n{'='*78}")
    print("  Esta es la LÍNEA BASE, y sale de señales que ya tenías.")
    print("  El LLM de la fase 5 no vale la pena si no supera estos números.")
    print("  Ojo también con lo contrario: si alguna señal diera una elevación")
    print("  de 8x, sospecha de un sesgo de anticipación antes que celebrar.")
    print(f"{'='*78}\n")


if __name__ == "__main__":
    main()
