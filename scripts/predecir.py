"""
predecir.py (emisor de predicciones)

Cada día registra, para cada activo, la probabilidad de que caiga más de un
umbral dentro del horizonte. Con fecha, con método y sin posibilidad de
reescribirla después: eso es lo que convierte una opinión sobre el futuro en
una apuesta que `resolver.py` puede cobrar.

Cuatro métodos compiten sobre las mismas fechas y activos:

  baseline_naive       Volatilidad histórica, sin regímenes.
  baseline_tendencia   Mezcla por régimen más calendario de eventos.
  llm_ajustado         Lo anterior, deformado por los impactos del grafo.
  regla_vix            VIX sobre su percentil 80 anual. Es el listón real
                       (2,08x de elevación con 41 % de cobertura fuera de
                       muestra) y nadie le ha ganado todavía.

Cómo funciona:

  · El umbral de cada activo son 1,2 desviaciones típicas suyas, no un
    porcentaje fijo, porque un 3 % no significa lo mismo en el VIX que en
    el dólar.
  · Los cuatro métodos comparten semilla, que depende del activo y del día
    pero nunca del método. Así la diferencia entre ellos es la del modelo y
    no ruido de muestreo.
  · La fecha de emisión sale de los datos (la moda de los últimos cierres)
    y no del reloj del servidor.

    python scripts/predecir.py
    python scripts/predecir.py --horizonte 5 --umbral 3
    python scripts/predecir.py --seco
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from calendario import factores_por_dia
from comun import TIPOS_OBJETIVO, conectar, leer_todo, log, upsert
from pronostico import matriz_transicion, prob_caida, proyectar, retornos

MODELO = "motor-v1"

# Peso del segundo aviso en adelante dentro de un mismo canal (el primero
# cuenta entero). Es una elección razonada, no medida: se podrá calibrar
# cuando el marcador tenga sucesos suficientes.
PESO_CORRELADO = 0.33


# ---------------------------------------------------------------------------
def ajuste_llm(sb, ticker: str, horizonte: int) -> tuple[float, float, int]:
    """
    Traduce los impactos vigentes del grafo a dos números: cuánto se ensancha
    la volatilidad y hacia qué lado engorda la cola.

    Los impactos se componen en VARIANZA, no multiplicándose: dos avisos de
    1,3 dan 1,41 y no 1,69. Pero solo los independientes se suman enteros.
    Dentro de un mismo canal describen el mismo mecanismo (cuatro titulares
    sobre el mismo acuerdo no son cuatro riesgos), así que el mayor cuenta
    entero y el resto lleva `PESO_CORRELADO`.

    Quedan fuera los marcados como `lote_uniforme`: tienen la cita
    verificada pero la atribución es relleno. Se conservan en la base para
    poder medir después si apartarlos fue correcto.
    """
    filas = (sb.table("impactos")
             .select("factor_incert,cola,intensidad_cola,confianza,"
                     "horizonte_d,creado_en,canal")
             .eq("ticker", ticker).eq("cita_verificada", True)
             # El `is.null` es necesario: las filas anteriores a la columna
             # la tienen a NULL, y en SQL `NULL = false` no es falso sino
             # desconocido, así que un `eq` a secas las excluiría todas.
             .or_("lote_uniforme.is.null,lote_uniforme.eq.false")
             .gte("horizonte_d", horizonte)
             .order("creado_en", desc=True).limit(40).execute().data)
    if not filas:
        return 1.0, 0.0, 0

    por_canal: dict[str, list[float]] = {}
    sesgo, peso_total = 0.0, 0.0

    for f in filas:
        conf = float(f.get("confianza") or 0.5)
        fac = float(f["factor_incert"])
        aporte = conf * max(0.0, fac ** 2 - 1)
        por_canal.setdefault(f.get("canal") or "?", []).append(aporte)

        signo = {"izquierda": -1.0, "derecha": 1.0}.get(f["cola"], 0.0)
        sesgo += conf * signo * float(f.get("intensidad_cola") or 0)
        peso_total += conf

    exceso = 0.0
    for aportes in por_canal.values():
        aportes.sort(reverse=True)
        exceso += aportes[0] + PESO_CORRELADO * sum(aportes[1:])

    factor = float(np.clip(np.sqrt(1 + exceso), 1.0, 2.5))
    sesgo_n = float(np.clip(sesgo / peso_total, -1, 1)) if peso_total else 0.0
    return factor, sesgo_n, len(filas)


def senal_vix(regimen_df: pd.DataFrame) -> tuple[bool, float]:
    """
    La regla de una línea que hay que batir: VIX sobre su percentil 80 anual.

    Devuelve (marca, probabilidad). La probabilidad no sale de un modelo
    sino de la frecuencia histórica medida: 23,4 % de acierto cuando marca,
    11,25 % de base cuando no. Una regla binaria puede competir en un
    marcador probabilístico si se le asigna su tasa observada.
    """
    if regimen_df.empty:
        return False, 0.1125
    vix = pd.to_numeric(regimen_df["vix_cierre"], errors="coerce").dropna()
    if len(vix) < 252:
        return False, 0.1125
    marca = bool(vix.iloc[-1] > vix.tail(252).quantile(0.80))
    return marca, (0.234 if marca else 0.0844)


# ---------------------------------------------------------------------------
def fecha_de_mercado(sb, tickers: list[str]) -> date:
    """
    Fecha del cierre en que se basa la predicción, deducida de los datos.

    No se usa `date.today()` porque el runner arranca a las 00:30 UTC, que
    son las 19:30 de Bogotá del día anterior: las tandas del viernes caían
    en sábado y dos corridas sobre el mismo cierre se duplicaban en vez de
    pisarse (la fecha entra en la clave de conflicto).

    Se toma la moda de los últimos cierres. No el máximo, porque la TRM se
    publica con vigencia futura; ni el mínimo, porque las bolsas asiáticas
    van un día por detrás.
    """
    salud = sb.table("v_salud_ingesta").select("ticker,ultimo_precio").execute().data
    objetivo = set(tickers)
    cierres = [r["ultimo_precio"] for r in salud
               if r["ticker"] in objetivo and r["ultimo_precio"]]
    if not cierres:
        print("  AVISO: no pude deducir la fecha de mercado; uso la del sistema.")
        return date.today()

    fecha, votos = Counter(cierres).most_common(1)[0]
    elegida = date.fromisoformat(str(fecha)[:10])

    if elegida != date.today():
        print(f"  Fecha de mercado: {elegida} "
              f"({votos} de {len(cierres)} activos cerraron ese día; "
              f"hoy en el servidor es {date.today()})")
    return elegida


def main() -> None:
    ap = argparse.ArgumentParser(description="Emisor de predicciones")
    ap.add_argument("--horizonte", type=int, default=5, help="días hábiles")
    ap.add_argument("--sigmas", type=float, default=1.2,
                    help="umbral en desviaciones típicas del propio activo")
    ap.add_argument("--umbral", type=float, default=None,
                    help="umbral absoluto en %%; anula --sigmas")
    ap.add_argument("--seco", action="store_true")
    ap.add_argument("--solo")
    a = ap.parse_args()

    sb = conectar()

    reg = pd.DataFrame(leer_todo(sb, "regimenes_mercado",
                                 "fecha,estado,vix_cierre", orden="fecha"))
    if reg.empty:
        sys.exit("Sin tabla de regímenes. Ejecuta ingestar_precios.py --solo-regimen")
    reg["fecha"] = pd.to_datetime(reg["fecha"])
    reg_serie = reg.set_index("fecha")["estado"]
    P = matriz_transicion(reg_serie)
    marca_vix, prob_vix = senal_vix(reg)

    activos = (sb.table("activos").select("ticker,nombre,tipo,frecuencia")
               .eq("activo", True).eq("verificado", True)
               .eq("frecuencia", "diaria")
               .in_("tipo", list(TIPOS_OBJETIVO)).execute().data)
    if a.solo:
        activos = [x for x in activos if x["ticker"] == a.solo.upper()]

    hoy = fecha_de_mercado(sb, [x["ticker"] for x in activos])

    criterio = (f"caída > {a.umbral:.1f} % (absoluto)" if a.umbral
                else f"caída > {a.sigmas:.1f} sigma del propio activo")
    print(f"\n{'='*80}")
    print(f"EMISIÓN  ·  {hoy}  ·  {criterio}  ·  {a.horizonte} días")
    print(f"Régimen hoy: {reg_serie.iloc[-1]}   ·   señal VIX: "
          f"{'ACTIVA' if marca_vix else 'inactiva'}"
          f"{'   [SECO]' if a.seco else ''}")
    print(f"{'='*80}")
    print(f"  {'ACTIVO':<12}{'umbral':>8}{'naive':>8}{'tendencia':>11}{'llm':>8}"
          f"{'vix':>7}   {'imp':>4}{'factor':>8}{'sesgo':>7}")
    print(f"  {'-'*76}")

    filas, sin_datos = [], []
    for act in activos:
        t = act["ticker"]
        precios = leer_todo(sb, "precios_diarios", "fecha,cierre",
                            filtros={"ticker": t}, orden="fecha")
        if len(precios) < 300:
            sin_datos.append(t)
            continue
        s = pd.Series([float(p["cierre"]) for p in precios],
                      index=pd.to_datetime([p["fecha"] for p in precios]))
        s = s[~s.index.duplicated(keep="last")].sort_index()

        # Umbral en sigmas del propio activo: así "caída" significa lo mismo
        # para NVIDIA que para el índice dólar.
        sigma_h = float(retornos(s).tail(504).std() * np.sqrt(a.horizonte))
        umbral = (a.umbral / 100) if a.umbral else max(0.005, a.sigmas * sigma_h)

        futuras = pd.bdate_range(s.index[-1] + pd.Timedelta(days=1),
                                 periods=a.horizonte)
        try:
            fe = factores_por_dia(sb, t, futuras)
        except Exception:
            fe = None

        factor, sesgo, n_imp = ajuste_llm(sb, t, a.horizonte)

        variantes = {
            "baseline_naive":     dict(regimen=None, P=None, factores_evento=None),
            "baseline_tendencia": dict(regimen=reg_serie, P=P, factores_evento=fe),
            "llm_ajustado":       dict(regimen=reg_serie, P=P, factores_evento=fe,
                                       factor_llm=factor, sesgo_cola=sesgo),
        }

        # Números aleatorios comunes: la semilla depende del activo y del
        # día, nunca del método. Con 3.000 trayectorias el ruido de muestreo
        # ronda los 0,55 puntos, suficiente para que dos métodos idénticos
        # parecieran distintos si cada uno sorteara por su cuenta.
        semilla = abs(hash((t, hoy.isoformat()))) % 2**31

        probs = {}
        for metodo, kw in variantes.items():
            pr = proyectar(s, a.horizonte, semilla=semilla, **kw)
            if pr is None:
                continue
            p = prob_caida(pr, umbral)
            probs[metodo] = p
            filas.append({
                "ticker": t, "emitida_en": hoy.isoformat(),
                "horizonte_d": a.horizonte, "metodo": metodo,
                "direccion": -1 if p > 0.5 else 0,
                "retorno_esp": round(float(pr["p50"][-1] / pr["s0"] - 1), 5),
                "banda_baja": round(float(pr["p10"][-1] / pr["s0"] - 1), 5),
                "banda_alta": round(float(pr["p90"][-1] / pr["s0"] - 1), 5),
                "prob_caida": round(p, 4), "umbral_caida": round(umbral, 4),
                "confianza": round(min(1.0, n_imp / 10) if metodo == "llm_ajustado"
                                   else 0.5, 3),
                "modelo": MODELO,
                "regimen_emision": str(reg_serie.iloc[-1]),
            })

        # La regla del VIX no simula nada: declara su tasa histórica.
        filas.append({
            "ticker": t, "emitida_en": hoy.isoformat(),
            "horizonte_d": a.horizonte, "metodo": "regla_vix",
            "direccion": -1 if marca_vix else 0,
            "prob_caida": round(prob_vix, 4), "umbral_caida": round(umbral, 4),
            "confianza": 0.5, "modelo": "vix_p80",
            "regimen_emision": str(reg_serie.iloc[-1]),
        })

        # Una pregunta casi imposible o casi segura no discrimina.
        p_ref = probs.get("baseline_tendencia", 0)
        aviso = "  <- trivial" if p_ref < 0.01 or p_ref > 0.60 else ""

        print(f"  {t:<12}{umbral*100:>7.1f}%"
              f"{probs.get('baseline_naive',0)*100:>7.1f}%"
              f"{probs.get('baseline_tendencia',0)*100:>10.1f}%"
              f"{probs.get('llm_ajustado',0)*100:>7.1f}%"
              f"{prob_vix*100:>6.1f}%   {n_imp:>4}{factor:>8.2f}{sesgo:>+7.2f}{aviso}")

    if sin_datos:
        print(f"\n  Sin histórico suficiente: {', '.join(sin_datos)}")

    if a.seco:
        print(f"\n  {len(filas)} predicciones calculadas, ninguna escrita.")
        return

    n = upsert(sb, "predicciones", filas,
               "ticker,emitida_en,horizonte_d,metodo,modelo")
    log(sb, "predecir", None, True, filas=n)
    print(f"\n  {n} predicciones registradas para el {hoy}.")
    print(f"\n  A partir de ahora son apuestas: dentro de {a.horizonte} días")
    print(f"  hábiles, `resolver.py` las cobra contra lo que pasó de verdad.")


if __name__ == "__main__":
    main()
