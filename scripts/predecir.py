"""
predecir.py — El emisor. Fase 6.

Cada día estampa, para cada activo, la probabilidad de que caiga más de un
umbral dentro del horizonte. Con fecha, con método y sin posibilidad de
reescribirla después.

Ese "sin posibilidad de reescribirla" es el punto entero. Todo lo anterior
—el grafo causal, el calendario, el espectro— produce afirmaciones sobre el
futuro que suenan razonables. Esto las convierte en apuestas registradas, y
`resolver.py` las cobra.

Cuatro métodos compiten sobre exactamente las mismas fechas y activos:

  baseline_naive       Volatilidad histórica, sin regímenes. El listón bajo.
  baseline_tendencia   Mezcla por régimen + calendario de eventos.
  llm_ajustado         Lo anterior deformado por los impactos del grafo.
  regla_vix            VIX por encima de su percentil 80 anual. El listón
                       de verdad: gana 2,08x de elevación con 41 % de
                       cobertura, y hasta hoy nadie le ha ganado.

    python scripts/predecir.py
    python scripts/predecir.py --horizonte 5 --umbral 3
    python scripts/predecir.py --seco
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from calendario import factores_por_dia
from comun import conectar, leer_todo, log, upsert
from pronostico import matriz_transicion, prob_caida, proyectar, retornos

MODELO = "motor-v1"


# ---------------------------------------------------------------------------
def ajuste_llm(sb, ticker: str, horizonte: int) -> tuple[float, float, int]:
    """
    Traduce los impactos vigentes del grafo a dos números.

    factor: cuánto se ensancha la volatilidad. Los impactos se componen en
    VARIANZA, no multiplicándose: dos avisos de 1,3 dan 1,41 y no 1,69,
    porque riesgos independientes se suman en varianza. Sin eso, cinco
    noticias mediocres producirían un factor absurdo.

    sesgo: hacia qué lado engorda la cola, de −1 a +1.

    Ambos se ponderan por la confianza declarada, y solo entran impactos
    con cita verificada cuyo horizonte no haya vencido.

    Y NO entran los marcados como `lote_uniforme`: abanicos y titulares
    estirados. Esas filas tienen la cita verificada —la frase existe— pero
    la atribución es relleno, y ensanchar una distribución real con ellas
    es peor que no tener noticia ninguna. Se quedan escritas en la base
    para que el marcador pueda decir si esta decisión fue correcta; lo que
    no hacen es mover un pronóstico mientras tanto.
    """
    filas = (sb.table("impactos")
             .select("factor_incert,cola,intensidad_cola,confianza,horizonte_d,creado_en")
             .eq("ticker", ticker).eq("cita_verificada", True)
             # `is.null` va incluido a propósito: las filas escritas antes de
             # que existiera la columna la tienen a NULL, y en SQL
             # `NULL = false` no es cierto sino desconocido. Con un `eq` a
             # secas, todo el historial anterior desaparecería del pronóstico
             # sin que nada avisara. Es el mismo fallo silencioso que el tope
             # de 1.000 filas de PostgREST: no da error, da menos datos.
             .or_("lote_uniforme.is.null,lote_uniforme.eq.false")
             .gte("horizonte_d", horizonte)
             .order("creado_en", desc=True).limit(40).execute().data)
    if not filas:
        return 1.0, 0.0, 0

    exceso, sesgo, peso_total = 0.0, 0.0, 0.0
    for f in filas:
        conf = float(f.get("confianza") or 0.5)
        fac = float(f["factor_incert"])
        exceso += conf * max(0.0, fac ** 2 - 1)
        signo = {"izquierda": -1.0, "derecha": 1.0}.get(f["cola"], 0.0)
        sesgo += conf * signo * float(f.get("intensidad_cola") or 0)
        peso_total += conf

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
    hoy = date.today()

    reg = pd.DataFrame(leer_todo(sb, "regimenes_mercado",
                                 "fecha,estado,vix_cierre", orden="fecha"))
    if reg.empty:
        sys.exit("Sin tabla de regímenes. Ejecuta ingestar_precios.py --solo-regimen")
    reg["fecha"] = pd.to_datetime(reg["fecha"])
    reg_serie = reg.set_index("fecha")["estado"]
    P = matriz_transicion(reg_serie)
    marca_vix, prob_vix = senal_vix(reg)

    # Solo activos de PRECIO. Las tasas y el VIX son variables explicativas
    # del modelo, no objetivos: preguntar por una "caída del 3 %" en el VIX
    # —que se mueve un 5 % en un día tranquilo— o en el rendimiento del
    # bono a 10 años no describe ningún evento. Meterlos en el marcador
    # sería promediar aciertos sobre preguntas que no significan lo mismo.
    TIPOS_OBJETIVO = ("accion", "indice", "etf", "materia_prima", "divisa")
    activos = (sb.table("activos").select("ticker,nombre,tipo,frecuencia")
               .eq("activo", True).eq("verificado", True)
               .eq("frecuencia", "diaria")
               .in_("tipo", list(TIPOS_OBJETIVO)).execute().data)
    if a.solo:
        activos = [x for x in activos if x["ticker"] == a.solo.upper()]

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

        # Umbral en unidades del propio activo. Así "caída" significa lo
        # mismo estadísticamente para NVIDIA que para el índice dólar, y el
        # marcador puede promediar entre activos sin mezclar preguntas
        # triviales con imposibles. Con 1,2 sigma el S&P sale en ~3 %, que
        # es justo el umbral con el que medimos la regla del VIX.
        sigma_h = float(retornos(s).tail(504).std() * np.sqrt(a.horizonte))
        umbral = (a.umbral / 100) if a.umbral else max(0.005, a.sigmas * sigma_h)

        futuras = pd.bdate_range(s.index[-1] + pd.Timedelta(days=1),
                                 periods=a.horizonte)
        try:
            fe = factores_por_dia(sb, t, futuras)
        except Exception:
            fe = None

        factor, sesgo, n_imp = ajuste_llm(sb, t, a.horizonte)

        # Los tres métodos ven exactamente la misma serie y el mismo día.
        # Solo cambia lo que cada uno decide mirar.
        variantes = {
            "baseline_naive":     dict(regimen=None, P=None, factores_evento=None),
            "baseline_tendencia": dict(regimen=reg_serie, P=P, factores_evento=fe),
            "llm_ajustado":       dict(regimen=reg_serie, P=P, factores_evento=fe,
                                       factor_llm=factor, sesgo_cola=sesgo),
        }

        # NÚMEROS ALEATORIOS COMUNES. La semilla depende del activo y del
        # día, NUNCA del método: los tres simulan con el mismo sorteo.
        #
        # Con 3.000 trayectorias, el error de muestreo sobre una
        # probabilidad del 10 % es de unos 0,55 puntos. Si cada método
        # usara su propia semilla, dos métodos IDÉNTICOS podrían diferir
        # un punto largo por puro azar, y el marcador atribuiría esa
        # diferencia al modelo. Compartir el sorteo cancela ese ruido y
        # deja solo la diferencia real.
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

        # Una pregunta cuya respuesta es "casi nunca" o "casi siempre" no
        # discrimina entre métodos: todos aciertan por el mismo motivo.
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
