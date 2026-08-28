"""
pronostico.py — El motor de simulación, en un solo sitio.

Estaba dentro de app.py. Sacarlo aquí no es orden por orden: si el gráfico
y el emisor de predicciones calculan el espectro con dos copias del mismo
código, tarde o temprano una se corrige y la otra no, y entonces el
marcador estará evaluando algo distinto de lo que el usuario vio en
pantalla. Una sola implementación, dos consumidores.

Contiene:
  · la cadena de Markov de regímenes estimada de los datos
  · la simulación de trayectorias con mezcla por régimen
  · el ajuste por eventos de calendario (frente 1 de la fase 5)
  · el ajuste por impactos del LLM   (frente 2 de la fase 5)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

ESTADOS = ["normal", "estres", "shock"]
TAU = 0.10          # desviación a priori de la deriva anual
N_SIM = 3000


# ---------------------------------------------------------------------------
def retornos(s: pd.Series) -> pd.Series:
    """
    Retornos logarítmicos saltándose precios no positivos.

    Hace falta porque el 20 de abril de 2020 el WTI cerró en −37,63 dólares.
    Fue real, y el logaritmo de un negativo no existe.
    """
    v = s.where(s > 0)
    return np.log(v / v.shift(1)).replace([np.inf, -np.inf], np.nan).dropna()


def matriz_transicion(regimen: pd.Series) -> np.ndarray:
    """
    Con qué probabilidad el mercado pasa de un régimen a otro mañana.

    Importa porque los shocks se agrupan: un día de pánico casi nunca viene
    solo. Sortear el régimen de forma independiente cada día borraría los
    tramos malos largos, que son los que hacen daño de verdad.
    """
    P = np.full((3, 3), 1 / 3)
    if regimen is None or regimen.empty:
        return P
    idx = {e: i for i, e in enumerate(ESTADOS)}
    v = [idx[e] for e in regimen.values if e in idx]
    C = np.zeros((3, 3))
    for a, b in zip(v, v[1:]):
        C[a, b] += 1
    for i in range(3):
        if C[i].sum() > 0:
            P[i] = C[i] / C[i].sum()
    return P


def inclinar(P: np.ndarray, sesgo: float) -> np.ndarray:
    """
    Inclina las transiciones hacia el estrés según el sesgo de cola del LLM.

    Es la forma más honesta que encontré de traducir "la cola izquierda
    engordó" a algo mecánico: las malas noticias no bajan el precio por
    decreto, hacen más probable entrar en un régimen agitado. `sesgo` va de
    −1 (todo el riesgo a la baja) a +1, y como máximo duplica o reduce a la
    mitad la probabilidad de salir del régimen tranquilo.
    """
    if abs(sesgo) < 0.01:
        return P
    Q = P.copy()
    f = 1.0 + max(-0.5, min(0.5, -sesgo))     # sesgo negativo -> f > 1
    for i in range(3):
        Q[i, 1] *= f
        Q[i, 2] *= f
        Q[i] = np.clip(Q[i], 0, None)
        s = Q[i].sum()
        if s > 0:
            Q[i] /= s
    return Q


# ---------------------------------------------------------------------------
def proyectar(serie: pd.Series, horizonte: int, *,
              regimen: pd.Series | None = None,
              P: np.ndarray | None = None,
              factores_evento: np.ndarray | None = None,
              modo_deriva: str = "cero",
              factor_llm: float = 1.0,
              sesgo_cola: float = 0.0,
              semilla: int = 11) -> dict | None:
    """
    Espectro de trayectorias posibles, no una línea.

    La deriva va a CERO por defecto. No es pereza: las 24 series del
    universo tienen tendencia histórica positiva, las 24 sin excepción, y
    eso describe la década 2016-2026, no a los activos. A horizontes de
    uno a seis meses el retorno pasado no predice al futuro.

    Los shocks no se borran del cálculo, se simulan. Filtrarlos triplicaba
    la deriva: el S&P pasaba de 13,4 % a 44,5 % anual.
    """
    limpia = serie.dropna()
    if len(limpia) < 250 or horizonte <= 0:
        return None

    ret = retornos(limpia)
    if ret.empty or ret.std() == 0:
        return None

    if P is None:
        P = matriz_transicion(regimen)
    P = inclinar(P, sesgo_cola)

    est = (regimen.reindex(ret.index) if regimen is not None
           else pd.Series(index=ret.index, dtype=object))
    mu_r, sd_r = np.zeros(3), np.zeros(3)
    for i, e in enumerate(ESTADOS):
        sub = ret[est.values == e] if not est.empty else ret.iloc[:0]
        if len(sub) >= 20:
            mu_r[i], sd_r[i] = sub.mean(), sub.std()
        else:
            mu_r[i], sd_r[i] = ret.mean(), ret.std()

    anios = len(ret) / 252
    mu_anual = ret.mean() * 252
    sd_anual = ret.std() * np.sqrt(252)
    ee_anual = sd_anual / np.sqrt(anios)
    peso = TAU ** 2 / (TAU ** 2 + ee_anual ** 2)
    mu_objetivo = mu_anual * peso if modo_deriva == "historica" else 0.0

    vals, vecs = np.linalg.eig(P.T)
    pi = np.real(vecs[:, np.argmin(np.abs(vals - 1))])
    pi = pi / pi.sum() if pi.sum() else np.full(3, 1 / 3)
    mu_r = mu_r + (mu_objetivo - float(pi @ mu_r) * 252) / 252

    # El ensanchamiento del LLM multiplica la volatilidad de todos los días;
    # el de calendario solo la del día del evento. Son cosas distintas: una
    # noticia cambia el clima, un dato programado es un salto puntual.
    sd_r = sd_r * float(factor_llm)

    fe = (np.ones(horizonte) if factores_evento is None
          else np.asarray(factores_evento, dtype=float)[:horizonte])
    if len(fe) < horizonte:
        fe = np.concatenate([fe, np.ones(horizonte - len(fe))])

    rng = np.random.default_rng(semilla)
    hoy = regimen.iloc[-1] if regimen is not None and len(regimen) else "normal"
    estado = np.full(N_SIM, ESTADOS.index(hoy) if hoy in ESTADOS else 0)
    log_s = np.zeros(N_SIM)
    caminos = np.empty((horizonte, N_SIM))
    acum = np.zeros(3)

    for d in range(horizonte):
        u = rng.random(N_SIM)
        cum = P[estado].cumsum(axis=1)
        estado = (u[:, None] > cum).sum(axis=1).clip(0, 2)
        for i in range(3):
            acum[i] += (estado == i).sum()
        log_s += rng.normal(mu_r[estado], sd_r[estado] * fe[d])
        caminos[d] = log_s

    s0 = float(limpia.iloc[-1])
    precios = s0 * np.exp(caminos)
    pcts = np.percentile(precios, [5, 10, 25, 50, 75, 90, 95], axis=1)
    minimos = precios.min(axis=0)        # el peor punto de cada trayectoria

    return {
        "fechas": pd.bdate_range(limpia.index[-1] + pd.Timedelta(days=1),
                                 periods=horizonte),
        "p05": pcts[0], "p10": pcts[1], "p25": pcts[2], "p50": pcts[3],
        "p75": pcts[4], "p90": pcts[5], "p95": pcts[6],
        "s0": s0,
        "minimos": minimos,
        "mu_bruta": (np.exp(mu_anual) - 1) * 100,
        "mu_usada": (np.exp(mu_objetivo) - 1) * 100,
        "encogimiento": peso,
        "sigma_anual": sd_anual * 100,
        "sd_por_regimen": sd_r * np.sqrt(252) * 100,
        "mu_por_regimen": (np.exp(mu_r * 252) - 1) * 100,
        "regimen_hoy": hoy,
        "prob_shock": acum[2] / (N_SIM * horizonte) * 100,
        "n": len(ret),
        "prob_subir": float((precios[-1] > s0).mean()) * 100,
        "dias_con_evento": int((fe > 1.001).sum()),
        "factor_evento_max": float(fe.max()),
        "factor_llm": float(factor_llm),
        "sesgo_cola": float(sesgo_cola),
        "modo_deriva": modo_deriva,
    }


def prob_caida(pr: dict, umbral: float) -> float:
    """
    Probabilidad de tocar una caída mayor que `umbral` (en tanto por uno) en
    algún momento del horizonte.

    Usa el MÍNIMO de cada trayectoria, no su punto final: una caída del 6 %
    que se recupera antes del vencimiento igual te habría dolido si estabas
    dentro. Es la misma definición que usa el evaluador, y tienen que serlo
    o el marcador compararía peras con manzanas.
    """
    return float((pr["minimos"] / pr["s0"] - 1 < -umbral).mean())
