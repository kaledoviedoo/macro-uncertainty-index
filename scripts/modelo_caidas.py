"""
modelo_caidas.py — El baseline honesto: un solo número, validado fuera de muestra.

`evaluar.py` mide señales una a una y en muestra. Esto hace las tres cosas
que faltaban:

  1. LAS COMBINA en una probabilidad única. Las señales sueltas se solapan
     —VIX alto, régimen estrés y vol reciente alta miden casi lo mismo—, así
     que sumarlas a ojo exagera lo que aportan.

  2. VALIDA FUERA DE MUESTRA con walk-forward: entrena solo con el pasado,
     predice hacia adelante, y nunca al revés. Con un embargo entre train y
     test, porque el objetivo de los últimos días del entrenamiento se
     resuelve dentro del período de prueba y eso es fuga de información.

  3. MIDE LA CALIBRACIÓN. Cuando el modelo dice 30 %, ¿ocurre el 30 % de
     las veces? Sin esto la probabilidad es un ranking, no un número.

Y corrige un problema de `evaluar.py` que infla la confianza: las ventanas
futuras se solapan, así que 2.514 días NO son 2.514 observaciones
independientes. Aquí los intervalos salen de un bootstrap por bloques.

    python scripts/modelo_caidas.py
    python scripts/modelo_caidas.py --ticker ^GSPC --caida 3 --dias 5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from comun import conectar, leer_todo
from evaluar import cargar, objetivo

try:
    from sklearn.linear_model import LogisticRegression
except ImportError:
    sys.exit(
        "Falta scikit-learn. Instálalo con el Python de ESTE entorno:\n\n"
        f"    {sys.executable} -m pip install scikit-learn\n\n"
        "(`pip` a secas no existe como comando en Windows; hay que\n"
        " invocarlo como módulo del intérprete que va a usarlo.)"
    )

BURN_IN = 500        # días mínimos antes de la primera predicción
PASO = 63            # reentrenar cada trimestre
COEFS: list = []     # coeficientes de cada reentrenamiento, para inspección


# ---------------------------------------------------------------------------
def caracteristicas(s: pd.Series, reg: pd.DataFrame, fechas_ev: set) -> pd.DataFrame:
    """
    Todas calculadas con información disponible el día t. Ni una mira adelante.

    Se usan versiones RELATIVAS (el VIX contra su propia mediana anual, la
    vol de 5 días contra la de 60) en vez de niveles absolutos. Un VIX de 20
    significaba algo distinto en 2017 que en 2022; el cociente contra su
    propia historia reciente es comparable a lo largo de la década.
    """
    v = s.where(s > 0)
    r = np.log(v / v.shift(1)).replace([np.inf, -np.inf], np.nan)

    vix = (pd.to_numeric(reg["vix_cierre"], errors="coerce").reindex(s.index)
           if not reg.empty else pd.Series(np.nan, index=s.index))
    estado = (reg["estado"].reindex(s.index)
              if not reg.empty else pd.Series(index=s.index, dtype=object))

    prox_ev = pd.Series(0.0, index=s.index)
    if fechas_ev:
        ev = pd.DatetimeIndex(sorted(fechas_ev))
        for i, d in enumerate(s.index):
            prox_ev.iloc[i] = float(((ev > d) & (ev <= d + pd.Timedelta(days=7))).sum())

    X = pd.DataFrame({
        "vix_rel":      vix / vix.rolling(252).median(),
        "vix_cambio":   vix / vix.shift(1) - 1,
        "vol_ratio":    r.rolling(5).std() / r.rolling(60).std(),
        "vol_60":       r.rolling(60).std() * np.sqrt(252),
        "drawdown_60":  s / s.rolling(60).max() - 1,
        "ret_5":        s / s.shift(5) - 1,
        "ret_20":       s / s.shift(20) - 1,
        "estres":       estado.eq("estres").astype(float),
        "shock":        estado.eq("shock").astype(float),
        "eventos_7d":   prox_ev,
    }, index=s.index)
    return X.replace([np.inf, -np.inf], np.nan)


# ---------------------------------------------------------------------------
def walk_forward(X: pd.DataFrame, y: pd.Series, dias: int) -> pd.Series:
    """
    Predicciones fuera de muestra, una tras otra hacia adelante.

    El embargo de `dias` filas es la parte que casi todo el mundo olvida: el
    objetivo del último día de entrenamiento se resuelve mirando precios que
    caen dentro del período de prueba. Sin quitarlas, el modelo ve de reojo
    el futuro que se le pide predecir y el resultado sale inflado.
    """
    pred = pd.Series(np.nan, index=X.index)
    n = len(X)
    i = BURN_IN
    while i < n:
        fin = min(i + PASO, n)
        corte = i - dias                       # embargo
        Xtr, ytr = X.iloc[:corte], y.iloc[:corte]
        ok = Xtr.notna().all(axis=1) & ytr.notna()
        if ok.sum() > 200 and ytr[ok].nunique() == 2:
            # SIN class_weight="balanced". Reponderar las clases mejora el
            # ranking pero destroza la calibración: el modelo aprende a
            # hablar como si las caídas fueran el 50 % de los días en vez
            # del 11 %, y devuelve 62 % donde la realidad es 19 %. Como la
            # calibración es una de las tres métricas que nos importan, la
            # máxima verosimilitud sin reponderar es lo correcto aquí.
            #
            # C=0.4: diez características sobre ~400 observaciones
            # efectivas es mucho. La regularización fuerte es lo que evita
            # que el modelo memorice la crisis de 2020.
            m = LogisticRegression(max_iter=2000, C=0.4)
            mu, sd = Xtr[ok].mean(), Xtr[ok].std().replace(0, 1)
            m.fit((Xtr[ok] - mu) / sd, ytr[ok].astype(int))
            COEFS.append(pd.Series(m.coef_[0], index=X.columns))

            Xte = X.iloc[i:fin]
            val = Xte.notna().all(axis=1)
            if val.any():
                p = m.predict_proba((Xte[val] - mu) / sd)[:, 1]
                pred.iloc[i:fin] = pd.Series(p, index=Xte[val].index).reindex(Xte.index)
        i = fin
    return pred


def bootstrap_bloques(y: np.ndarray, marca: np.ndarray, base: float,
                      bloque: int = 21, n_rep: int = 2000, semilla: int = 7):
    """
    Intervalo del 90 % para la elevación, remuestreando BLOQUES de días.

    Hace falta porque los días vecinos comparten casi toda su ventana
    futura: 2.514 días con objetivo a 5 días valen como unas 500
    observaciones independientes, no 2.514. Remuestrear días sueltos daría
    un intervalo mucho más estrecho de lo real.
    """
    rng = np.random.default_rng(semilla)
    n = len(y)
    n_bloques = max(1, n // bloque)
    out = []
    for _ in range(n_rep):
        inicios = rng.integers(0, max(1, n - bloque), n_bloques)
        idx = np.concatenate([np.arange(s, min(s + bloque, n)) for s in inicios])
        m, t = marca[idx], y[idx]
        if m.sum() < 10:
            continue
        out.append((t[m].mean() / base) if base > 0 else 0)
    return (np.percentile(out, 5), np.percentile(out, 95)) if out else (np.nan, np.nan)


# ---------------------------------------------------------------------------
def informe(ticker: str, p: pd.Series, y: pd.Series, dias: int, caida: float,
            referencia: pd.Series | None = None):
    ok = p.notna() & y.notna()
    p, y = p[ok], y[ok].astype(int)
    if len(p) < 200:
        sys.exit("Muy pocas predicciones fuera de muestra.")
    base = y.mean()

    print(f"\n{'='*78}")
    print(f"{ticker}  ·  caída > {caida:.0f} % en {dias} días  ·  FUERA DE MUESTRA")
    print(f"{'='*78}")
    print(f"  Predicciones evaluadas  {len(p):,}   ({p.index.min():%Y-%m} a {p.index.max():%Y-%m})")
    print(f"  Frecuencia base         {base*100:.2f} %")

    brier = ((p - y) ** 2).mean()
    brier_base = ((base - y) ** 2).mean()
    print(f"  Brier del modelo        {brier:.4f}")
    print(f"  Brier de la base        {brier_base:.4f}")
    mejora = (1 - brier / brier_base) * 100
    print(f"  Mejora sobre la base    {mejora:+.1f} %"
          f"   {'(el modelo aporta)' if mejora > 0 else '(el modelo ESTORBA)'}")

    print(f"\n  CALIBRACIÓN  (¿cuando dice X %, pasa X %?)")
    print(f"  {'probabilidad dicha':<22}{'n':>6}{'observado':>11}{'error':>9}")
    q = pd.qcut(p, 5, duplicates="drop")
    errores = []
    for tramo, g in y.groupby(q, observed=True):
        dicho = p[g.index].mean()
        obs = g.mean()
        errores.append(abs(dicho - obs) * len(g))
        print(f"  {dicho*100:>8.1f} %{'':<12}{len(g):>6}{obs*100:>10.1f} %"
              f"{(obs-dicho)*100:>+8.1f}")
    ece = sum(errores) / len(y) * 100
    print(f"\n  Error de calibración esperado: {ece:.2f} puntos")
    print(f"  {'Bien calibrado (< 5 pts).' if ece < 5 else 'Mal calibrado: el número no se puede usar como probabilidad.'}")

    print(f"\n  ELEVACIÓN POR UMBRAL DE AVISO")
    print(f"  {'umbral':>8}{'marca':>8}{'acierta':>9}{'elevac':>8}"
          f"{'cobert':>8}   IC 90 % de la elevación")
    for pct in (50, 70, 80, 90):
        u = np.percentile(p, pct)
        marca = (p >= u).values
        if marca.sum() < 20:
            continue
        acierta = y.values[marca].mean()
        elev = acierta / base if base else 0
        cob = y.values[marca].sum() / y.sum()
        lo, hi = bootstrap_bloques(y.values.astype(bool), marca, base)
        aviso = "  <<<" if lo > 1.0 else ""
        print(f"  p{pct:<7}{marca.mean()*100:>7.0f}%{acierta*100:>8.1f}%"
              f"{elev:>7.2f}x{cob*100:>7.0f}%     [{lo:.2f}x – {hi:.2f}x]{aviso}")

    print(f"\n  <<< = el extremo bajo del intervalo supera 1x, o sea que la")
    print(f"        elevación sobrevive al solapamiento de ventanas.")
    print(f"        Ese intervalo sale de un bootstrap por bloques de 21 días:")
    print(f"        los días vecinos comparten su ventana futura, así que")
    print(f"        {len(p):,} días valen como ~{len(p)//dias:,} observaciones reales.")

    # La prueba de la navaja: ¿el modelo de diez variables le gana a una
    # regla de una línea, medidos AMBOS en el mismo período fuera de
    # muestra? Si no le gana, la regla simple es la respuesta correcta:
    # no tiene parámetros que ajustar, no se degrada y se explica sola.
    if referencia is not None:
        ref = referencia.reindex(p.index).fillna(False).astype(bool).values
        if ref.sum() >= 20:
            acierta = y.values[ref].mean()
            elev = acierta / base if base else 0
            cob = y.values[ref].sum() / y.sum()
            lo, hi = bootstrap_bloques(y.values.astype(bool), ref, base)
            # A IGUAL TASA DE AVISO. Comparar el p90 del modelo (marca 10 %
            # de los días) contra una regla que marca el 20 % es hacer
            # trampa a favor del modelo: marcar menos días sube la
            # precisión y baja la cobertura, así que no son la misma
            # apuesta. Se iguala la tasa y entonces sí se pueden mirar.
            tasa = ref.mean()
            um = np.percentile(p, (1 - tasa) * 100)
            mm = (p >= um).values
            elev_m = (y.values[mm].mean() / base) if base else 0
            cob_m = y.values[mm].sum() / y.sum()
            lo_m, hi_m = bootstrap_bloques(y.values.astype(bool), mm, base)

            print(f"\n  LA NAVAJA DE OCCAM  ·  a igual tasa de aviso ({tasa*100:.0f} % de los días)")
            print(f"  {'':<26}{'acierta':>9}{'elevac':>8}{'cobert':>8}   IC 90 %")
            print(f"  {'VIX > su p80 anual':<26}{acierta*100:>8.1f}%{elev:>7.2f}x"
                  f"{cob*100:>7.0f}%     [{lo:.2f}x – {hi:.2f}x]")
            print(f"  {'modelo de 10 variables':<26}{y.values[mm].mean()*100:>8.1f}%"
                  f"{elev_m:>7.2f}x{cob_m*100:>7.0f}%     [{lo_m:.2f}x – {hi_m:.2f}x]")

            if elev >= elev_m:
                print(f"\n  GANA LA REGLA DE UNA LÍNEA: {elev:.2f}x contra {elev_m:.2f}x,")
                print(f"  y con {cob*100:.0f} % de cobertura contra {cob_m*100:.0f} %.")
                print(f"  Diez variables, un walk-forward y una regresión no le")
                print(f"  ganan a `VIX > su percentil 80`. Úsala en producción: no")
                print(f"  tiene parámetros que ajustar, no se degrada con el tiempo")
                print(f"  y se explica en una frase.")
            else:
                print(f"\n  El modelo aporta {(elev_m/elev-1)*100:+.0f} % de elevación sobre la")
                print(f"  regla simple, a igual tasa de aviso. Ese es su valor real.")

    if COEFS:
        med = pd.concat(COEFS, axis=1).mean(axis=1).sort_values(key=abs, ascending=False)
        print(f"\n  PESO MEDIO DE CADA CARACTERÍSTICA  ({len(COEFS)} reentrenamientos)")
        for k, v in med.head(6).items():
            print(f"    {k:<16}{v:+.3f}")
        print("  Signo positivo = empuja hacia 'va a caer'.")

    return brier, ece


def main() -> None:
    ap = argparse.ArgumentParser(description="Baseline de caídas fuera de muestra")
    ap.add_argument("--ticker", default="^GSPC")
    ap.add_argument("--caida", type=float, default=3.0)
    ap.add_argument("--dias", type=int, default=5)
    a = ap.parse_args()

    sb = conectar(silencioso=True)
    s, reg, ev = cargar(sb, a.ticker)
    X = caracteristicas(s, reg, ev)
    # objetivo() ya devuelve NaN donde la ventana futura está incompleta.
    y = objetivo(s, a.dias, a.caida)

    # La regla de referencia, calculada igual que en evaluar.py.
    vix = (pd.to_numeric(reg["vix_cierre"], errors="coerce").reindex(s.index)
           if not reg.empty else pd.Series(np.nan, index=s.index))
    ref = vix > vix.rolling(252).quantile(0.80)

    print(f"\n{a.ticker}: {len(s):,} días · {X.shape[1]} características")
    p = walk_forward(X, y, a.dias)
    informe(a.ticker, p, y, a.dias, a.caida, referencia=ref)

    print(f"\n{'='*78}")
    print("  ESTE es el número que el LLM tiene que superar en la fase 5.")
    print("  No la exactitud, no el acierto direccional: la elevación fuera")
    print("  de muestra con su intervalo, y el error de calibración.")
    print(f"{'='*78}\n")


if __name__ == "__main__":
    main()
