"""
resolver.py (cobrador y marcador)

Busca las predicciones cuyo horizonte ya venció, las enfrenta a lo que pasó
de verdad, y publica el marcador. Es la mitad del proyecto que decide si la
otra mitad sirve.

Cómo funciona:

  · Se resuelve por el MÍNIMO de la ventana, no por el cierre final. Una
    caída del 6 % que se recupera antes de vencer sigue siendo una caída, y
    es la misma definición que usó el emisor.
  · No se resuelve nada sin ventana completa. Si al activo le faltan días de
    precio, la predicción sigue abierta. Dar por "no cayó" lo que aún no se
    sabe es contar el silencio como acierto.
  · El precio de referencia es el último cierre disponible al emitir, así
    que una predicción fechada en fin de semana usa el viernes.

El marcador se calcula aquí, en Python, y no en una vista de la base: cada
número necesita su definición escrita al lado, y una vista que no viaja con
el repositorio no se puede auditar. Cuenta apuestas distintas y no filas
(los cuatro métodos opinan sobre el mismo activo y día), y se niega a
ordenar métodos con menos de 20 caídas resueltas, porque con menos el Brier
premia al que declara la probabilidad más baja y no al que acierta.

    python scripts/resolver.py
    python scripts/resolver.py --marcador
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
import pandas as pd
from comun import conectar, leer_todo, log


# ---------------------------------------------------------------------------
def resolver(sb) -> int:
    abiertas = (sb.table("predicciones")
                .select("id,ticker,emitida_en,horizonte_d,umbral_caida,"
                        "banda_baja,banda_alta,metodo")
                .is_("resuelta_el", "null")
                .order("emitida_en").limit(5000).execute().data)
    if not abiertas:
        print("  No hay predicciones abiertas.")
        return 0

    print(f"\n{'='*76}\nRESOLVIENDO  ·  {len(abiertas)} predicciones abiertas\n{'='*76}")

    series: dict[str, pd.Series] = {}
    regimen = pd.DataFrame(leer_todo(sb, "regimenes_mercado", "fecha,estado",
                                     orden="fecha"))
    if not regimen.empty:
        regimen["fecha"] = pd.to_datetime(regimen["fecha"])
        regimen = regimen.set_index("fecha")["estado"]

    resueltas, pendientes = [], 0
    for p in abiertas:
        t = p["ticker"]
        if t not in series:
            filas = leer_todo(sb, "precios_diarios", "fecha,cierre",
                              filtros={"ticker": t}, orden="fecha")
            s = pd.Series([float(f["cierre"]) for f in filas],
                          index=pd.to_datetime([f["fecha"] for f in filas]))
            series[t] = s[~s.index.duplicated(keep="last")].sort_index()
        s = series[t]
        if s.empty:
            continue

        emitida = pd.Timestamp(p["emitida_en"])
        # El precio de referencia es el último cierre DISPONIBLE al emitir,
        # no el de la fecha de emisión: si se emitió en fin de semana, el
        # último cierre es el del viernes.
        previos = s[s.index <= emitida]
        futuros = s[s.index > emitida]
        if previos.empty:
            continue

        h = int(p["horizonte_d"])
        if len(futuros) < h:
            pendientes += 1          # ventana incompleta: sigue abierta
            continue

        s0 = float(previos.iloc[-1])
        ventana = futuros.iloc[:h]
        ret_final = float(ventana.iloc[-1] / s0 - 1)
        ret_min = float(ventana.min() / s0 - 1)
        umbral = float(p["umbral_caida"] or 0.03)
        cayo = ret_min < -umbral

        dentro = None
        if p["banda_baja"] is not None and p["banda_alta"] is not None:
            dentro = bool(float(p["banda_baja"]) <= ret_final <= float(p["banda_alta"]))

        estado_fin = None
        if not regimen.empty:
            trozo = regimen[(regimen.index > emitida) & (regimen.index <= ventana.index[-1])]
            if len(trozo):
                # El peor régimen alcanzado manda: una predicción emitida en
                # calma y resuelta en shock queda etiquetada como tal.
                for e in ("shock", "estres", "normal"):
                    if (trozo == e).any():
                        estado_fin = e
                        break

        resueltas.append({
            "id": p["id"],
            "retorno_real": round(ret_final, 5),
            "retorno_min": round(ret_min, 5),
            "cayo": cayo,
            "acertada": dentro,
            "resuelta_el": date.today().isoformat(),
            "resuelta_en": ventana.index[-1].date().isoformat(),
            "regimen_resol": estado_fin,
        })

    for r in resueltas:
        rid = r.pop("id")
        sb.table("predicciones").update(r).eq("id", rid).execute()

    log(sb, "resolver", None, True, filas=len(resueltas))
    print(f"  {len(resueltas)} resueltas   ·   {pendientes} siguen abiertas "
          f"(ventana futura incompleta)")
    return len(resueltas)


# ---------------------------------------------------------------------------
# Fracción que cada método "marca" al compararlos. Ninguno avisa por su
# cuenta, así que se toma su 20 % de mayor probabilidad declarada, igual para
# todos: comparar aciertos sin igualar la tasa hace ganar al más selectivo.
TASA_DE_MARCADO = 0.20

# Por debajo de esto no se publica ranking: con dos o tres sucesos cualquier
# orden es ruido, y una tabla ordenada invita a leer un ganador que no hay.
MIN_EVENTOS_PARA_RANKING = 20


def _leer_resueltas(sb) -> list[dict]:
    """Todas las predicciones cerradas, paginadas."""
    filas, desde = [], 0
    while True:
        trozo = (sb.table("predicciones")
                 .select("metodo,ticker,emitida_en,horizonte_d,prob_caida,"
                         "umbral_caida,cayo,acertada,regimen_resol")
                 .not_.is_("resuelta_el", "null")
                 .order("emitida_en").range(desde, desde + 999).execute().data)
        filas.extend(trozo)
        if len(trozo) < 1000:
            return filas
        desde += 1000


def marcador(sb) -> None:
    """Marcador por horizonte: calibración siempre, ranking solo con datos."""
    filas = _leer_resueltas(sb)
    if not filas:
        print(f"\n{'='*76}")
        print("  Todavía no hay predicciones resueltas.")
        print("  Es lo normal: emite hoy, vuelve dentro de una semana.")
        print(f"{'='*76}\n")
        return

    print(f"\n{'='*76}\nMARCADOR\n{'='*76}")

    for h in sorted({f["horizonte_d"] for f in filas}):
        grupo = [f for f in filas if f["horizonte_d"] == h]

        # Sobre apuestas distintas, no sobre filas: los cuatro métodos
        # opinan sobre el mismo (ticker, día).
        apuestas = {(f["ticker"], f["emitida_en"]): bool(f["cayo"]) for f in grupo}
        eventos = sum(apuestas.values())
        base = eventos / len(apuestas) if apuestas else 0.0

        print(f"\n  Horizonte {h} días")
        print(f"  {len(grupo)} filas   ·   {len(apuestas)} apuestas distintas"
              f"   ·   {eventos} caídas   ·   frecuencia base {base*100:.1f} %")

        if eventos < MIN_EVENTOS_PARA_RANKING:
            fechas = sorted({f["emitida_en"] for f in grupo})
            print(f"\n  SIN RANKING. Hacen falta {MIN_EVENTOS_PARA_RANKING} "
                  f"caídas para ordenar métodos y hay {eventos}.")
            print(f"  Emisiones cerradas: {fechas[0]} a {fechas[-1]} "
                  f"({len(fechas)} días).")
            print()
            print("  Con tan pocos sucesos, cualquier orden entre métodos es")
            print("  ruido. Y ordenar por Brier cuando casi nada ha ocurrido")
            print("  premia al que declara la probabilidad más baja, no al que")
            print("  acierta: un método mudo que dijera siempre 0 % ganaría.")
            print()
            print("  Lo único que se puede leer hoy es la calibración: si un")
            print("  método declara 15 % y la frecuencia real es 2 %, sobra")
            print("  incertidumbre en el modelo, y eso sí se ve con pocos datos.")
            _tabla_calibracion(grupo, base)
            continue

        _tabla_ranking(grupo, base)

    print(f"\n  El listón: `regla_vix` da 2,08x de elevación con 41 % de")
    print(f"  cobertura fuera de muestra. Un método que no le gane no entra")
    print(f"  en producción, por convincentes que suenen sus cadenas causales.\n")


def _por_metodo(grupo: list[dict]) -> dict[str, list[dict]]:
    d: dict[str, list[dict]] = {}
    for f in grupo:
        d.setdefault(f["metodo"], []).append(f)
    return d


def _tabla_calibracion(grupo: list[dict], base: float) -> None:
    """Lo poco que se puede afirmar con pocos sucesos: calibración."""
    print(f"\n  {'MÉTODO':<22}{'n':>5}{'declara':>10}{'ocurrió':>10}"
          f"{'sesgo':>9}{'banda 80%':>11}")
    print(f"  {'-'*67}")

    for metodo, fs in sorted(_por_metodo(grupo).items()):
        probs = [float(f["prob_caida"] or 0) for f in fs]
        media = sum(probs) / len(probs)
        real = sum(bool(f["cayo"]) for f in fs) / len(fs)

        # La banda p10-p90 es la unica metrica con datos suficientes desde
        # el principio: cada prediccion la pone a prueba, haya caida o no.
        con_banda = [f for f in fs if f["acertada"] is not None]
        cob = (sum(bool(f["acertada"]) for f in con_banda) / len(con_banda)
               if con_banda else None)
        txt_cob = f"{cob*100:>9.0f} %" if cob is not None else "        —"

        print(f"  {metodo:<22}{len(fs):>5}{media*100:>9.1f}%{real*100:>9.1f}%"
              f"{(media-real)*100:>+8.1f}p{txt_cob}")

    print()
    print("  declara   = probabilidad media que el método puso sobre la mesa")
    print("  ocurrió   = frecuencia real de caídas en esas mismas apuestas")
    print("  sesgo     = declara menos ocurrió. Positivo = exceso de miedo")
    print("  banda 80% = cuántas veces el resultado cayó dentro de p10-p90.")
    print("              Debería rondar el 80 %. Muy por encima, las bandas")
    print("              son tan anchas que no dicen nada; muy por debajo,")
    print("              el modelo subestima la cola.")


def _tabla_ranking(grupo: list[dict], base: float) -> None:
    """El marcador de verdad, cuando ya hay sucesos que repartir."""
    print(f"\n  {'MÉTODO':<22}{'n':>5}{'declara':>9}{'brier':>9}"
          f"{'marca':>7}{'acierta':>9}{'elevac':>8}{'banda':>8}")
    print(f"  {'-'*77}")

    resultados = []
    for metodo, fs in _por_metodo(grupo).items():
        probs = [float(f["prob_caida"] or 0) for f in fs]
        cayos = [bool(f["cayo"]) for f in fs]
        brier = sum((p - c) ** 2 for p, c in zip(probs, cayos)) / len(fs)

        k = max(1, int(round(len(fs) * TASA_DE_MARCADO)))
        orden = sorted(zip(probs, cayos), key=lambda x: -x[0])[:k]
        acierta = sum(c for _, c in orden) / k
        elev = acierta / base if base else 0.0

        # Si declara siempre lo mismo, su "top 20 %" es un corte arbitrario.
        plano = max(probs) - min(probs) < 1e-9

        con_banda = [f for f in fs if f["acertada"] is not None]
        cob = (sum(bool(f["acertada"]) for f in con_banda) / len(con_banda)
               if con_banda else None)

        resultados.append((metodo, len(fs), sum(probs) / len(fs), brier,
                           k, acierta, elev, plano, cob))

    for (m, n, media, brier, k, ac, elev, plano, cob) in sorted(
            resultados, key=lambda r: -r[6]):
        txt_elev = "     —" if plano else f"{elev:>6.2f}x"
        txt_cob = f"{cob*100:>6.0f} %" if cob is not None else "      —"
        print(f"  {m:<22}{n:>5}{media*100:>8.1f}%{brier:>9.4f}"
              f"{k:>7}{ac*100:>8.1f}%{txt_elev}{txt_cob}")

    print()
    print(f"  marca   = las {TASA_DE_MARCADO*100:.0f} % con mayor probabilidad "
          f"declarada. Igual para todos:")
    print("            comparar aciertos sin igualar cuánto marca cada uno")
    print("            hace ganar al más selectivo, no al que más sabe.")
    print("  elevac  = acierta dividido por la frecuencia base. 1.00x = da")
    print("            igual que marcar al azar. Es la cifra que decide.")
    print("  brier   = error cuadrático medio de la probabilidad; menor mejor.")
    print("            No ordena la tabla: con pocos sucesos premia al tímido.")
    print("  —       = el método declara siempre lo mismo; no discrimina.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Resolutor y marcador")
    ap.add_argument("--marcador", action="store_true",
                    help="solo mostrar el marcador, sin resolver")
    a = ap.parse_args()

    sb = conectar()
    if not a.marcador:
        resolver(sb)
    marcador(sb)


if __name__ == "__main__":
    main()
