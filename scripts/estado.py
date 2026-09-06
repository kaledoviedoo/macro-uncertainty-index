"""
estado.py: El panel de control de la base, desde la terminal.

Las consultas SQL que fui dejando no se ejecutan en PowerShell: `select` allí
es `Select-Object`, un cmdlet que no tiene nada que ver. O las pegas en el
editor SQL del panel de Supabase, o usas esto.

    python scripts/estado.py
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from comun import conectar, leer_todo


def titulo(t: str) -> None:
    print(f"\n{'='*74}\n{t}\n{'='*74}")


def main() -> None:
    sb = conectar(silencioso=True)

    # ---------------------------------------------------------------- activos
    titulo("COBERTURA DE PRECIOS")
    salud = sb.table("v_salud_ingesta").select("*").execute().data
    salud.sort(key=lambda r: (r["dias_de_retraso"] is None,
                              -(r["dias_de_retraso"] or 0)))

    # El retraso solo significa algo comparado con la frecuencia de la serie.
    # Una mensual publicada en julio lleva 28 días de retraso el 28 de agosto
    # y está perfectamente al día; con el umbral diario salía "rezagado" cada
    # mes. Una alarma que suena siempre es una alarma que se deja de mirar, y
    # entonces tapa a la de al lado que sí importa.
    UMBRALES = {                 # frecuencia: (al día hasta, rezagado hasta)
        "diaria":   (4, 35),
        "semanal":  (10, 45),
        "mensual":  (45, 75),
        "trimestral": (135, 200),
    }
    frecuencias = {a["ticker"]: (a.get("frecuencia") or "diaria")
                   for a in sb.table("activos").select("ticker,frecuencia")
                             .execute().data}

    print(f"  {'TICKER':<12}{'ÚLTIMO DATO':<14}{'RETRASO':>9}{'FILAS':>9}  ESTADO")
    for r in salud:
        retraso = r["dias_de_retraso"]
        frec = frecuencias.get(r["ticker"], "diaria")
        al_dia, rezagado = UMBRALES.get(frec, UMBRALES["diaria"])
        sufijo = "" if frec == "diaria" else f" ({frec})"

        if retraso is None:
            marca, txt = "SIN DATOS", "—"
        elif retraso < 0:
            # La TRM se publica con fecha de aplicación futura: el valor de
            # hoy rige hasta el próximo día hábil. No es un error, es su
            # convención, y conviene que se lea como tal en vez de como -2d.
            marca, txt = "al día (fecha de vigencia)", f"+{-retraso}d"
        elif retraso <= al_dia:
            marca, txt = f"al día{sufijo}", f"{retraso}d"
        elif retraso <= rezagado:
            marca, txt = f"rezagado{sufijo}", f"{retraso}d"
        else:
            marca, txt = f"PARADO{sufijo}", f"{retraso}d"
        print(f"  {r['ticker']:<12}{str(r['ultimo_precio'] or '—'):<14}"
              f"{txt:>9}{r['filas_totales']:>9,}  {marca}")

    total = sum(r["filas_totales"] for r in salud)
    print(f"\n  {len(salud)} activos, {total:,} filas de precio.")

    # ----------------------------------------------------------------- huecos
    titulo("HUECOS EN LAS SERIES")
    huecos = sb.table("v_huecos_series").select("*").limit(20).execute().data
    if not huecos:
        print("  Ninguno. Todas las series son continuas dentro de su frecuencia.")
    else:
        print("  Un hueco no es un dato que falta: es un retorno falso esperando")
        print("  a que alguien lo calcule. Nada debe cruzar estos tramos.\n")
        for h in huecos:
            print(f"  {h['ticker']:<10}{h['desde']} → {h['hasta']}"
                  f"   {h['dias']:>4} días   ({h['frecuencia']})")

    # ---------------------------------------------------------------- régimen
    titulo("RÉGIMEN DE MERCADO")
    reg = leer_todo(sb, "regimenes_mercado", "fecha,estado", orden="fecha")
    if not reg:
        print("  Sin clasificar. Ejecuta ingestar_precios.py --solo-regimen")
    else:
        c = Counter(r["estado"] for r in reg)
        for estado in ("normal", "estres", "shock"):
            n = c.get(estado, 0)
            barra = "█" * round(40 * n / len(reg))
            print(f"  {estado:<8}{n:>6,} días  {100*n/len(reg):5.1f}%  {barra}")
        print(f"\n  {len(reg):,} días, de {reg[0]['fecha']} a {reg[-1]['fecha']}.")
        print(f"  Hoy: {reg[-1]['estado']}")

    # ---------------------------------------------------------------- fuentes
    titulo("FUENTES DE NOTICIAS")
    fuentes = sb.table("fuentes").select(
        "nombre,nivel_confianza,activa,url_verificada,ua_perfil"
    ).order("nivel_confianza").execute().data

    vivas = [f for f in fuentes if f["activa"] and f["url_verificada"]]
    print(f"  {len(vivas)} vivas de {len(fuentes)} registradas.\n")
    for f in vivas:
        print(f"  N{f['nivel_confianza']}  {f['nombre'][:44]:<46}[{f['ua_perfil'] or '?'}]")

    caidas = [f for f in fuentes if not f["activa"]]
    if caidas:
        print(f"\n  Descartadas ({len(caidas)}):")
        for f in caidas:
            print(f"      {f['nombre'][:52]}")

    # ------------------------------------------------------------------- log
    titulo("ÚLTIMOS FALLOS REGISTRADOS")
    fallos = (sb.table("ingesta_log").select("proceso,ticker,ejecutado_en,error")
              .eq("exito", False).order("ejecutado_en", desc=True)
              .limit(8).execute().data)
    if not fallos:
        print("  Ninguno.")
    else:
        for f in fallos:
            cuando = f["ejecutado_en"][:16].replace("T", " ")
            print(f"  {cuando}  {f['proceso']:<20}{(f['ticker'] or '—'):<10}"
                  f"{(f['error'] or '')[:60]}")

    # ------------------------------------------------------------ predicciones
    titulo("MARCADOR DEL MOTOR")
    marcador = sb.table("v_precision_por_metodo").select("*").execute().data
    if not marcador:
        abiertas = (sb.table("predicciones").select("id", count="exact")
                    .is_("resuelta_en", "null").limit(1).execute())
        n = getattr(abiertas, "count", None) or 0
        print(f"  Todavía no hay predicciones resueltas. {n} abiertas.")
        print("  Se cobran a 5 días hábiles: hasta que la ventana no cierre,")
        print("  `resolver.py` se niega a puntuarlas. Negarse es correcto:")
        print("  resolver con datos incompletos infla el marcador solo.")
        print("\n  Este número es el que decide si el motor sirve o solo")
        print("  suena convincente. Hasta que exista, todo lo demás es")
        print("  fontanería.")
    else:
        print(f"  {'MÉTODO':<22}{'HORIZ':>6}{'RESUELTAS':>11}{'ACIERTO':>9}")
        for m in marcador:
            print(f"  {m['metodo']:<22}{m['horizonte_d']:>5}d"
                  f"{m['resueltas']:>11,}{m['tasa_acierto_pct'] or 0:>8.1f}%")

    print()


if __name__ == "__main__":
    main()
