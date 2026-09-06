"""
sinapsis.py (inspector del grafo causal)

Muestra, para cada activo, qué noticias están ensanchando su cono ahora
mismo: la cita literal ya verificada, la fuente, y la aritmética completa
que produce el factor. Sin esto el motor solo afirma que el crudo se
ensancha un 77 %, y no hay forma de comprobarlo desde fuera.

Cómo funciona:

  · Lee los impactos con cita verificada cuyo horizonte aún cubre la
    ventana, y descarta los marcados como relleno (`lote_uniforme`).
  · Los agrupa por canal, que es la unidad en la que se componen: dentro
    de un canal el mayor cuenta entero y el resto lleva descuento, porque
    describen el mismo mecanismo.
  · Imprime la descomposición para que el factor se pueda recalcular a
    mano, y avisa si el número que sale aquí no coincide con el que
    aplica el motor.

El factor NO se recalcula en este archivo: se importa `ajuste_llm` de
`predecir.py`, que es la función que lo usa de verdad. Un inspector con su
propia copia de la fórmula acabaría enseñando un número que el motor no
aplica, que es peor que no tener inspector.

    python scripts/sinapsis.py
    python scripts/sinapsis.py --ticker CL=F
    python scripts/sinapsis.py --apartados
    python scripts/sinapsis.py --todos
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from comun import conectar, leer_todo
from predecir import PESO_CORRELADO, ajuste_llm

FLECHA = {"izquierda": "↓", "derecha": "↑", "ninguna": "="}


def envolver(texto: str, sangria: str, ancho: int = 74) -> str:
    return textwrap.fill(texto, width=ancho, initial_indent=sangria,
                         subsequent_indent=sangria)


# ---------------------------------------------------------------------------
def cargar(sb, horizonte: int) -> tuple[list[dict], dict, dict, dict]:
    """Impactos vigentes + los diccionarios para darles nombre."""
    impactos = leer_todo(
        sb, "impactos",
        "ticker,canal,horizonte_d,factor_incert,cola,intensidad_cola,cita,"
        "confianza,noticia_id,lote_uniforme,long_documento,creado_en,"
        "cita_verificada,razonamiento",
        orden="creado_en")

    impactos = [i for i in impactos
                if i.get("cita_verificada")
                and int(i.get("horizonte_d") or 0) >= horizonte]

    ids = {i["noticia_id"] for i in impactos}
    noticias, fuentes = {}, {}
    if ids:
        for n in leer_todo(sb, "noticias", "id,titular,publicado_en,fuente_id"):
            if n["id"] in ids:
                noticias[n["id"]] = n
        for f in sb.table("fuentes").select("id,nombre,nivel_confianza").execute().data:
            fuentes[f["id"]] = f

    activos = {a["ticker"]: a for a in
               sb.table("activos").select("ticker,nombre,tipo").execute().data}
    return impactos, noticias, fuentes, activos


def pintar_impacto(imp: dict, noticias: dict, fuentes: dict,
                   exceso_canal: float) -> None:
    """Una arista del grafo. El porcentaje es dentro de SU canal,
    porque el canal es la unidad en la que se componen."""
    conf = float(imp.get("confianza") or 0.5)
    fac = float(imp["factor_incert"])

    aporte = conf * max(0.0, fac ** 2 - 1)
    parte = (f"{aporte / exceso_canal * 100:.0f} % de su canal"
             if exceso_canal > 0 else "—")

    flecha = FLECHA.get(imp.get("cola") or "ninguna", "=")
    print(f"     x{fac:.2f} {flecha} {(imp.get('canal') or '?'):<18}"
          f"{imp.get('horizonte_d', 0):>4}d   conf {conf:.2f}"
          f"   aporta {aporte:.3f}  ({parte})")

    n = noticias.get(imp["noticia_id"], {})
    f = fuentes.get(n.get("fuente_id"), {})
    nivel = f.get("nivel_confianza")
    etiqueta = f"N{nivel}" if nivel is not None else "?"
    largo = imp.get("long_documento")
    tam = f"  ·  {largo:,} car." if largo else ""
    print(f"        {f.get('nombre', 'fuente desconocida')[:44]}  ·  {etiqueta}"
          f"  ·  {str(n.get('publicado_en', ''))[:10]}{tam}")

    cita = (imp.get("cita") or "").strip()
    if cita:
        print(envolver(f"«{cita}»", "        "))

    razon = (imp.get("razonamiento") or "").strip()
    if razon:
        print(envolver(f"-> {razon}", "        "))
    print()


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="El grafo causal vigente")
    ap.add_argument("--ticker", help="solo este activo")
    ap.add_argument("--horizonte", type=int, default=5)
    ap.add_argument("--apartados", action="store_true",
                    help="enseña las filas marcadas como relleno")
    ap.add_argument("--todos", action="store_true",
                    help="incluye activos sin impactos vigentes")
    a = ap.parse_args()

    sb = conectar(silencioso=True)
    impactos, noticias, fuentes, activos = cargar(sb, a.horizonte)

    if a.ticker:
        t = a.ticker.upper()
        impactos = [i for i in impactos if i["ticker"] == t]

    # Se separan antes de agrupar: no cuentan para el factor.
    vigentes = [i for i in impactos if not i.get("lote_uniforme")]
    apartados = [i for i in impactos if i.get("lote_uniforme")]

    por_ticker: dict[str, list[dict]] = {}
    for i in vigentes:
        por_ticker.setdefault(i["ticker"], []).append(i)

    print(f"\n{'='*78}")
    print(f"SINAPSIS  ·  grafo causal vigente  ·  horizonte {a.horizonte} días")
    print(f"{'='*78}")
    print(f"  {len(vigentes)} impactos activos sobre {len(por_ticker)} activos"
          f"   ·   {len(apartados)} apartados por relleno")

    if not por_ticker:
        print("\n  Ningún impacto vigente. O no hay noticias procesadas, o el")
        print("  modelo no encontró nada que citara: las dos son respuestas")
        print("  legítimas, y el silencio es preferible a un análisis inventado.")
        print(f"{'='*78}\n")
        return

    orden = sorted(por_ticker.items(), key=lambda kv: -len(kv[1]))
    if a.todos:
        for t in activos:
            por_ticker.setdefault(t, [])
        orden = sorted(por_ticker.items(), key=lambda kv: -len(kv[1]))

    for ticker, imps in orden:
        nombre = activos.get(ticker, {}).get("nombre", "")
        factor, sesgo, n = ajuste_llm(sb, ticker, a.horizonte)

        print(f"\n{'-'*78}")
        print(f"  {ticker:<12} {nombre[:46]}")
        print(f"  factor {factor:.2f}   sesgo {sesgo:+.2f}   ·   "
              f"{len(imps)} impacto(s) vigente(s)")
        print()

        if not imps:
            print("     Sin noticias que lo toquen. El pronóstico es puramente")
            print("     estadístico: `llm_ajustado` vale lo mismo que")
            print("     `baseline_tendencia`, hasta el decimal.\n")
            continue

        # Se agrupan por canal porque así es como se componen.
        por_canal: dict[str, list[float]] = {}
        for i in imps:
            conf = float(i.get("confianza") or 0.5)
            fac = float(i["factor_incert"])
            por_canal.setdefault(i.get("canal") or "?", []).append(
                conf * max(0.0, fac ** 2 - 1))

        orden_canal = {c: sum(v) for c, v in por_canal.items()}
        for imp in sorted(imps, key=lambda i: (
                -orden_canal.get(i.get("canal") or "?", 0),
                i.get("canal") or "?",
                -float(i["factor_incert"]))):
            canal = imp.get("canal") or "?"
            pintar_impacto(imp, noticias, fuentes, sum(por_canal[canal]))

        exceso = 0.0
        print("     composición por canal   "
              f"(el mayor entero, el resto al {PESO_CORRELADO*100:.0f} %):")
        for canal, aportes in sorted(orden_canal.items(), key=lambda kv: -kv[1]):
            xs = sorted(por_canal[canal], reverse=True)
            resto = sum(xs[1:])
            sub = xs[0] + PESO_CORRELADO * resto
            exceso += sub
            detalle = (f"{xs[0]:.3f} + {PESO_CORRELADO}·{resto:.3f}"
                       if resto else f"{xs[0]:.3f}")
            print(f"       {canal:<20} {detalle:>22}  =  {sub:.3f}")

        crudo = (1 + exceso) ** 0.5
        print(f"       {'exceso total':<20} {exceso:>22.3f}")
        print(f"     raíz(1 + {exceso:.3f}) = {crudo:.2f}", end="")
        if crudo > 2.5 + 1e-9:
            print(f"   [recortado a {factor:.2f}: tope de 2,50]")
        else:
            print()
        if abs(crudo - factor) > 0.005 and crudo <= 2.5:
            print(f"     AVISO: el motor aplica {factor:.2f} y aquí sale "
                  f"{crudo:.2f}. Revisa `ajuste_llm`.")
        print(f"     Se componen en VARIANZA, no multiplicándose: dos avisos")
        print(f"     de 1,30 dan 1,41 y no 1,69. Y dentro de un canal se")
        print(f"     descuentan, porque describen el mismo mecanismo.")

    # -------------------------------------------------------------------
    if apartados and a.apartados:
        print(f"\n{'='*78}")
        print(f"APARTADOS  ·  {len(apartados)} filas fuera del pronóstico")
        print(f"{'='*78}")
        print("  Tienen la cita verificada (la frase existe) pero la atribución")
        print("  es relleno: abanico (varios activos, un canal, factores en")
        print("  escalera) o titular sin cuerpo estirado a varias filas.")
        print("  Siguen en la base para poder medir si apartarlas fue correcto.\n")
        for imp in sorted(apartados, key=lambda i: i["ticker"]):
            n = noticias.get(imp["noticia_id"], {})
            print(f"  {imp['ticker']:<11} x{float(imp['factor_incert']):.2f} "
                  f"{(imp.get('canal') or '?'):<16} "
                  f"{str(n.get('titular', ''))[:40]}")
    elif apartados:
        print(f"\n  ({len(apartados)} impactos apartados por relleno. "
              f"Añade --apartados para verlos.)")

    print(f"\n{'='*78}")
    print("  Cada cita de arriba se comprobó LITERALMENTE contra el documento")
    print("  antes de escribirse: coincidencia exacta o 92 % de cobertura de")
    print("  tokens en ventana. Que la frase exista es una garantía mecánica.")
    print("  Que la atribución sea correcta, no: eso lo dirá el marcador.")
    print(f"{'='*78}\n")


if __name__ == "__main__":
    main()
