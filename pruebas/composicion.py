"""
Pruebas de cómo se componen los impactos en un factor.

Se ejercitan las dos correcciones del 2026-09-06 sobre el caso que las
motivó: los trece impactos reales del petróleo, que producían un factor de
2,26 del que el 68 % venía de titulares del FT de menos de 220 caracteres.

    python pruebas/composicion.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from modelos import CONF_MAX_TITULAR, MIN_CUERPO_PARA_VARIOS, techo_de_titular
from predecir import PESO_CORRELADO, ajuste_llm

fallos = []


def check(nombre, cond, detalle=""):
    print(f"  {'OK  ' if cond else 'FALLA'}  {nombre}{'   ' + detalle if detalle else ''}")
    if not cond:
        fallos.append(nombre)


# ---------------------------------------------------------------------------
# CL=F el 2026-09-06, tal como lo imprimió sinapsis.
# (factor, confianza declarada, canal, cola, intensidad, caracteres del doc)
CRUDO = [
    (1.30, 0.85, "oferta",             "izquierda", 0.3, 2444),  # EIA
    (1.30, 0.85, "oferta",             "izquierda", 0.3,  150),  # FT Venezuela
    (1.30, 0.50, "riesgo_geopolitico", "derecha",   0.3,  182),  # FT Iran
    (1.25, 0.85, "oferta",             "ninguna",   0.0,  100),  # FT China
    (1.25, 0.85, "oferta",             "derecha",   0.3,  179),  # FT escalada
    (1.20, 0.50, "demanda",            "ninguna",   0.0, 9222),  # BCE
    (1.15, 0.50, "riesgo_geopolitico", "derecha",   0.2,  146),  # FT Sri Lanka
    (1.15, 0.85, "oferta",             "izquierda", 0.3,  177),  # FT Chevron
    (1.15, 0.60, "oferta",             "izquierda", 0.2,  113),  # FT capitalismo
    (1.15, 0.50, "oferta",             "ninguna",   0.0,  192),  # FT Trump-linked
    (1.15, 0.90, "oferta",             "derecha",   0.3, 2764),  # EIA crack
    (1.12, 0.90, "oferta",             "izquierda", 0.3, 2164),  # EIA hidrocarburos
    (1.08, 0.50, "oferta",             "derecha",   0.2,  218),  # FT clima
]


def filas(aplicar_techo: bool) -> list[dict]:
    out = []
    for fac, conf, canal, cola, inten, largo in CRUDO:
        c = techo_de_titular(conf, largo)[0] if aplicar_techo else conf
        out.append({"factor_incert": fac, "confianza": c, "canal": canal,
                    "cola": cola, "intensidad_cola": inten, "horizonte_d": 30,
                    "creado_en": "2026-09-06", "lote_uniforme": False})
    return out


class TablaFalsa:
    def __init__(self, datos):
        self._todo = datos

    def table(self, _):        self._d = list(self._todo); return self
    def select(self, *a, **k): return self
    def eq(self, *a, **k):     return self
    def or_(self, *a, **k):    return self
    def gte(self, *a, **k):    return self
    def order(self, *a, **k):  return self
    def limit(self, *a, **k):  return self
    def execute(self):         return type("R", (), {"data": self._d})()


def factor_de(datos):
    return ajuste_llm(TablaFalsa(datos), "CL=F", 5)


def suma_plana(fs):
    """Lo que hacía el motor antes: todo sumado como si fuera independiente."""
    return sum(f["confianza"] * max(0.0, f["factor_incert"] ** 2 - 1) for f in fs)


# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print("EL CASO DEL PETRÓLEO — 13 impactos, 9 de ellos titulares del FT")
print("=" * 74)

antes = suma_plana(filas(False))
f_antes = (1 + antes) ** 0.5
print(f"\n  Antes (todo entero, sin techo)")
print(f"     suma {antes:.3f}   ->   factor {f_antes:.2f}")

solo_techo = suma_plana(filas(True))
print(f"\n  Solo con el techo de confianza a titulares")
print(f"     suma {solo_techo:.3f}   ->   factor {(1 + solo_techo) ** 0.5:.2f}")

f_final, sesgo, n = factor_de(filas(True))
print(f"\n  Con techo + descuento por correlación (peso {PESO_CORRELADO})")
print(f"     factor {f_final:.2f}   ·   sesgo {sesgo:+.2f}   ·   {n} impactos")

print(f"\n  {f_antes:.2f}  ->  {f_final:.2f}"
      f"   ({(1 - f_final / f_antes) * 100:.0f} % menos de ensanchamiento)")

check("el caso real reproduce el 2,26 de partida", abs(f_antes - 2.26) < 0.02,
      f"-> {f_antes:.2f}")
check("el factor baja de forma sustancial", f_final < 1.85, f"-> {f_final:.2f}")
check("pero NO se anula: las noticias siguen contando", f_final > 1.30,
      f"-> {f_final:.2f}")

# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print("EL TECHO — un titular no afirma con la misma fuerza que un documento")
print("=" * 74)

c, r = techo_de_titular(0.90, 150)
check("un titular de 150 car. se recorta", c == CONF_MAX_TITULAR and r,
      f"-> 0.90 pasa a {c}")

c, r = techo_de_titular(0.90, 2444)
check("un documento con cuerpo no se toca", c == 0.90 and not r)

c, r = techo_de_titular(0.20, 150)
check("una confianza ya baja no SUBE al techo", c == 0.20 and not r,
      "-> el techo recorta, nunca infla")

c, r = techo_de_titular(0.90, MIN_CUERPO_PARA_VARIOS)
check("el umbral es inclusivo", c == 0.90 and not r,
      f"-> {MIN_CUERPO_PARA_VARIOS} car. ya cuenta como documento")

# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print("EL DESCUENTO — la misma historia contada cuatro veces es una historia")
print("=" * 74)

# Cuatro avisos idénticos por el mismo canal frente a uno solo.
uno = [{"factor_incert": 1.30, "confianza": 0.85, "canal": "oferta",
        "cola": "ninguna", "intensidad_cola": 0.0, "horizonte_d": 30,
        "creado_en": "x", "lote_uniforme": False}]
cuatro = uno * 4

f1 = factor_de(uno)[0]
f4 = factor_de(cuatro)[0]
print(f"\n  1 aviso de x1.30            ->  factor {f1:.3f}")
print(f"  4 avisos idénticos, mismo canal ->  factor {f4:.3f}")

plano4 = (1 + suma_plana(cuatro)) ** 0.5
print(f"  (sin descuento habrían dado     ->  factor {plano4:.3f})")

check("cuatro avisos correlados no cuadruplican", f4 < plano4,
      f"-> {f4:.3f} frente a {plano4:.3f}")
check("pero siguen aportando más que uno solo", f4 > f1,
      f"-> {f4:.3f} > {f1:.3f}")

# Canales distintos: SÍ son independientes y no llevan descuento.
distintos = [dict(uno[0], canal=c) for c in
             ("oferta", "demanda", "costes", "regulacion")]
fd = factor_de(distintos)[0]
print(f"\n  4 avisos en canales DISTINTOS   ->  factor {fd:.3f}")
check("canales distintos no se descuentan", abs(fd - plano4) < 1e-9,
      f"-> {fd:.3f} = suma entera")
check("y pesan más que los correlados", fd > f4, f"-> {fd:.3f} > {f4:.3f}")

print("\n" + "=" * 74)
print(f"{len(fallos)} fallos" if fallos else "TODO PASA")
print("=" * 74 + "\n")
sys.exit(1 if fallos else 0)
