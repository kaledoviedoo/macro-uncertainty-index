"""
resolver.py — El cobrador. Fase 6.

Busca las predicciones cuyo horizonte ya venció y las enfrenta a lo que
pasó. Después imprime el marcador.

Dos decisiones que determinan si el marcador es honesto:

  SE RESUELVE POR EL MÍNIMO, no por el cierre final. Una caída del 6 % que
  se recupera antes del vencimiento sigue siendo una caída que te habría
  dolido si estabas dentro. Es la misma definición que usó el emisor: si
  no coincidieran, estaríamos evaluando una pregunta distinta de la que
  se hizo.

  NO SE RESUELVE NADA SIN VENTANA COMPLETA. Si al activo le faltan días de
  precio para cubrir el horizonte, la predicción sigue abierta. Dar por
  "no cayó" lo que todavía no se sabe es contar el silencio como acierto,
  que es exactamente el sesgo que hundió el objetivo del 88 %.

    python scripts/resolver.py
    python scripts/resolver.py --marcador     # solo muestra, no resuelve
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
def marcador(sb) -> None:
    filas = sb.table("v_marcador").select("*").execute().data
    if not filas:
        print(f"\n{'='*76}")
        print("  Todavía no hay predicciones resueltas.")
        print("  Es lo normal: emite hoy, vuelve dentro de una semana.")
        print(f"{'='*76}\n")
        return

    print(f"\n{'='*76}\nMARCADOR\n{'='*76}")
    for h in sorted({f["horizonte_d"] for f in filas}):
        grupo = [f for f in filas if f["horizonte_d"] == h]
        base = float(grupo[0]["frecuencia_base"] or 0)
        print(f"\n  Horizonte {h} días   ·   frecuencia base {base*100:.1f} %")
        print(f"  {'MÉTODO':<22}{'n':>6}{'prob.dice':>11}{'ocurrió':>10}"
              f"{'avisos':>8}{'acierto':>9}{'elevac':>8}{'brier':>9}")
        print(f"  {'-'*74}")

        ref_brier = next((float(g["brier"]) for g in grupo
                          if g["metodo"] == "baseline_naive"), None)

        for g in sorted(grupo, key=lambda x: -(float(x["acierto_en_avisos"] or 0))):
            ac = float(g["acierto_en_avisos"] or 0)
            elev = ac / base if base else 0
            brier = float(g["brier"] or 0)
            mejor = ""
            if ref_brier and g["metodo"] != "baseline_naive":
                mejor = " *" if brier < ref_brier else ""
            print(f"  {g['metodo']:<22}{g['resueltas']:>6}"
                  f"{float(g['prob_media_declarada'] or 0)*100:>10.1f}%"
                  f"{float(g['frecuencia_observada'] or 0)*100:>9.1f}%"
                  f"{g['avisos']:>8}{ac*100:>8.1f}%{elev:>7.2f}x"
                  f"{brier:>9.4f}{mejor}")

    print(f"\n  prob.dice = probabilidad media que declaró el método")
    print(f"  ocurrió   = frecuencia real. Si difieren mucho, está mal calibrado.")
    print(f"  elevac    = acierto en sus avisos, dividido por la frecuencia base")
    print(f"  brier     = error cuadrático de la probabilidad. Menor es mejor.")
    print(f"  *         = supera el Brier de baseline_naive")
    print(f"\n  El listón: `regla_vix` da 2,08x de elevación con 41 % de cobertura")
    print(f"  fuera de muestra. Un método que no le gane no entra en producción,")
    print(f"  por muy convincentes que suenen sus cadenas causales.\n")


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
