"""
calendario.py — Frente 1 de la fase 5: eventos de fecha conocida.

Sin LLM, sin adivinación. Las fechas del FOMC, del IPC y de la junta del
Banrep se publican con meses de antelación, y cuánto se mueve cada activo
en esos días es un promedio histórico, no una opinión.

Es la mejor relación valor/riesgo del proyecto: no puede alucinar.

    python scripts/calendario.py --sembrar      # tipos + fechas conocidas
    python scripts/calendario.py --calibrar     # mide el impacto histórico
    python scripts/calendario.py --proximos
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from comun import conectar, leer_todo, log, upsert

# Umbrales para decidir si un efecto es real o ruido.
# Con ~25 activos por tipo de evento, algunos van a dar factores altos por
# puro azar: exigir las tres condiciones a la vez es lo que evita construir
# el calendario sobre coincidencias.
MIN_EVENTOS = 8        # menos que esto no es una muestra
MAX_P = 0.05           # el efecto debe distinguirse del ruido
MIN_FACTOR = 1.15      # y ser lo bastante grande como para importar


TIPOS = [
    ("FOMC",      "Decisión de tasas de la Reserva Federal", "US", "Federal Reserve",
     None, "Ocho reuniones al año. Mueve tasas, dólar y bolsa a la vez."),
    ("CPI_US",    "IPC de Estados Unidos", "US", "BLS",
     None, "Suele publicarse a mitad de mes, 08:30 ET."),
    ("EMPLEO_US", "Informe de empleo (nóminas no agrícolas)", "US", "BLS",
     "primer viernes del mes", "08:30 ET. Junto al IPC, el dato que más mueve."),
    ("PPI_US",    "Precios al productor de EE.UU.", "US", "BLS", None, None),
    ("BANREP",    "Junta directiva del Banco de la República", "CO", "Banrep",
     None, "Decisión de la tasa de política. Mueve TRM, IBR y COLCAP."),
    ("BCE",       "Decisión de tasas del Banco Central Europeo", "EU", "ECB", None, None),
    ("OPEP",      "Reunión de la OPEP+", "INT", "OPEC",
     None, "Cuotas de producción. Mueve Brent y WTI."),
]


# ---------------------------------------------------------------------------
def sembrar(sb) -> None:
    print(f"\n{'='*70}\nSEMBRANDO TIPOS DE EVENTO\n{'='*70}")
    filas = [{"codigo": c, "nombre": n, "pais": p, "emisor": e,
              "regla_fecha": r, "descripcion": d} for c, n, p, e, r, d in TIPOS]
    upsert(sb, "tipos_evento", filas, "codigo")
    print(f"  {len(filas)} tipos registrados.")

    # Fechas derivables por regla. El informe de empleo de EE.UU. sale el
    # primer viernes de cada mes salvo excepciones raras: se marca como no
    # confirmado precisamente porque es una derivación, no el calendario.
    # Desde 2016 y no desde 2024: la calibración necesita eventos DENTRO del
    # histórico de precios. Con 29 fechas útiles los factores salían de 1,3 a
    # 1,6 pero con p de 0,06 a 0,13 — el efecto estaba ahí y no se distinguía
    # del ruido por falta de muestra. Con diez años son ~120 eventos y la
    # potencia del contraste se multiplica por dos.
    print("\n  Derivando fechas del informe de empleo (regla: primer viernes)…")
    eventos = []
    d = date(2016, 1, 1)
    fin = date.today() + timedelta(days=400)
    while d < fin:
        primero = date(d.year, d.month, 1)
        # weekday(): lunes=0 … viernes=4
        offset = (4 - primero.weekday()) % 7
        eventos.append({"tipo": "EMPLEO_US",
                        "fecha": (primero + timedelta(days=offset)).isoformat(),
                        "hora_utc": "13:30", "confirmado": False,
                        "nota": "derivada por regla, verificar contra bls.gov"})
        d = (primero + timedelta(days=32)).replace(day=1)

    n = upsert(sb, "eventos_calendario", eventos, "tipo,fecha")
    print(f"  {n} fechas de empleo entre {eventos[0]['fecha'][:4]} y {fin.year}.")
    log(sb, "calendario_sembrar", None, True, filas=n)

    print("\n  Los demás tipos necesitan su calendario oficial importado:")
    print("    FOMC    federalreserve.gov/monetarypolicy/fomccalendars.htm")
    print("    CPI/PPI bls.gov/schedule/news_release")
    print("    BANREP  banrep.gov.co  (calendario de juntas)")
    print("  Sin fechas cargadas, ese tipo simplemente no ensancha nada.")


# ---------------------------------------------------------------------------
def calibrar(sb) -> None:
    """
    Mide, activo por activo y evento por evento, cuánto más se mueve el
    precio ese día frente a un día cualquiera.

    El estadístico es deliberadamente simple: media del valor absoluto del
    retorno. No estima dirección —eso ya sabemos que no se puede— sino
    magnitud, que es lo que sí se estima bien.
    """
    print(f"\n{'='*70}\nCALIBRANDO IMPACTO HISTÓRICO\n{'='*70}")

    eventos = pd.DataFrame(leer_todo(sb, "eventos_calendario", "tipo,fecha",
                                     orden="fecha"))
    if eventos.empty:
        sys.exit("No hay eventos en el calendario. Ejecuta --sembrar primero.")
    eventos["fecha"] = pd.to_datetime(eventos["fecha"])

    activos = (sb.table("activos").select("ticker,nombre")
               .eq("activo", True).eq("verificado", True).execute().data)

    filas, descartados = [], 0
    for tipo, grupo in eventos.groupby("tipo"):
        fechas_ev = set(grupo["fecha"])
        print(f"\n  {tipo}  ({len(fechas_ev)} fechas)")

        for a in activos:
            t = a["ticker"]
            precios = leer_todo(sb, "precios_diarios", "fecha,cierre",
                                filtros={"ticker": t}, orden="fecha")
            if len(precios) < 300:
                continue
            s = pd.Series([float(p["cierre"]) for p in precios],
                          index=pd.to_datetime([p["fecha"] for p in precios]))
            # .where(s > 0): el 20-abr-2020 el WTI cerró en −37,63. Es un
            # precio real, y el logaritmo de un negativo no existe.
            v = s.where(s > 0)
            r = np.log(v / v.shift(1)).replace([np.inf, -np.inf], np.nan).dropna()
            if r.empty:
                continue

            marca = r.index.isin(fechas_ev)
            en_ev, fuera = r[marca], r[~marca]
            if len(en_ev) < MIN_EVENTOS or len(fuera) < 100:
                continue

            m_ev, m_base = en_ev.abs().mean(), fuera.abs().mean()
            if m_base == 0:
                continue
            factor = m_ev / m_base

            # Welch sobre |retorno|: ¿la diferencia se distingue del ruido?
            ee = np.sqrt(en_ev.abs().var() / len(en_ev)
                         + fuera.abs().var() / len(fuera))
            z = (m_ev - m_base) / ee if ee > 0 else 0.0
            # p a dos colas por aproximación normal, sin dependencias extra.
            # math.erf, no np.math: ese alias desapareció en NumPy 2.
            p = math.erfc(abs(z) / math.sqrt(2))

            fila = {
                "tipo": tipo, "ticker": t, "n_eventos": int(len(en_ev)),
                "mov_abs_evento": round(float(m_ev), 6),
                "mov_abs_base": round(float(m_base), 6),
                "factor": round(float(factor), 4),
                "p_valor": round(float(p), 6),
                "sesgo_negativo": round(float((en_ev < 0).mean()), 4),
            }
            filas.append(fila)

            if factor >= MIN_FACTOR and p < MAX_P:
                print(f"    {t:<12} factor {factor:5.2f}   p={p:.4f}   "
                      f"n={len(en_ev):>3}   neg={fila['sesgo_negativo']*100:.0f}%")
            else:
                descartados += 1

    if not filas:
        sys.exit("\n  Sin resultados. ¿Hay fechas de evento dentro del histórico?")

    n = upsert(sb, "impacto_evento", filas, "tipo,ticker")
    log(sb, "calendario_calibrar", None, True, filas=n)
    aplicables = sum(1 for f in filas
                     if f["factor"] >= MIN_FACTOR and f["p_valor"] < MAX_P)
    print(f"\n  {n} pares tipo×activo calibrados.")
    print(f"  {aplicables} superan los tres filtros y ensancharán la banda.")
    print(f"  {descartados} guardados pero inactivos: efecto pequeño o ruido.")
    print("\n  Se guardan también los descartados a propósito: saber que un")
    print("  evento NO mueve a un activo es información, y evita volver a")
    print("  calcularlo cada vez que dudes.")


# ---------------------------------------------------------------------------
def factores_por_dia(sb, ticker: str, fechas: pd.DatetimeIndex) -> np.ndarray:
    """
    ESTA ES LA FUNCIÓN QUE PIDES.

    Devuelve, para cada día futuro, el multiplicador que hay que aplicar a
    la volatilidad de ese día. 1.0 = día normal. 1.8 = ese día hay FOMC y
    este activo se mueve históricamente un 80 % más.

    Se aplica dentro del bucle de simulación, no al final: ensanchar solo
    el día del evento y dejar los demás intactos es lo que produce el
    escalón característico en el abanico, en vez de inflarlo entero.

    Reglas de aplicación:
      · Solo pares que superen los tres filtros (n, p, tamaño).
      · Si varios eventos caen el mismo día, los factores se componen en
        raíz cuadrática: dos eventos de 1.4 dan 1.56, no 1.96. Los riesgos
        independientes se suman en varianza, no en desviación.
      · Techo de 3.0 por si la calibración devuelve algo absurdo.
    """
    factores = np.ones(len(fechas))
    if len(fechas) == 0:
        return factores

    impactos = (sb.table("impacto_evento")
                .select("tipo,factor,p_valor,n_eventos")
                .eq("ticker", ticker)
                .gte("factor", MIN_FACTOR)
                .lt("p_valor", MAX_P)
                .gte("n_eventos", MIN_EVENTOS)
                .execute().data)
    if not impactos:
        return factores
    por_tipo = {i["tipo"]: float(i["factor"]) for i in impactos}

    eventos = (sb.table("eventos_calendario").select("tipo,fecha")
               .gte("fecha", fechas[0].date().isoformat())
               .lte("fecha", fechas[-1].date().isoformat())
               .in_("tipo", list(por_tipo))
               .execute().data)
    if not eventos:
        return factores

    pos = {f.date(): i for i, f in enumerate(fechas)}
    exceso = np.zeros(len(fechas))          # se acumula en varianza
    for e in eventos:
        i = pos.get(date.fromisoformat(e["fecha"]))
        if i is not None:
            exceso[i] += (por_tipo[e["tipo"]] ** 2 - 1)

    return np.clip(np.sqrt(1 + np.maximum(exceso, 0)), 1.0, 3.0)


def proximos(sb) -> None:
    print(f"\n{'='*70}\nPRÓXIMOS 90 DÍAS\n{'='*70}")
    ev = sb.table("v_eventos_proximos").select("*").execute().data
    if not ev:
        print("  Nada en el calendario. Ejecuta --sembrar.")
        return
    for e in ev:
        marca = "" if e["confirmado"] else "  (fecha derivada)"
        print(f"  {e['fecha']}  en {e['en_dias']:>3}d   {e['tipo']:<11}"
              f"{e['activos_afectados']:>3} activos afectados{marca}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Calendario de eventos")
    ap.add_argument("--sembrar", action="store_true")
    ap.add_argument("--calibrar", action="store_true")
    ap.add_argument("--proximos", action="store_true")
    a = ap.parse_args()

    sb = conectar()
    if a.sembrar:
        sembrar(sb)
    if a.calibrar:
        calibrar(sb)
    if a.proximos or not (a.sembrar or a.calibrar):
        proximos(sb)


if __name__ == "__main__":
    main()
