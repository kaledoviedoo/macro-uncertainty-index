"""
Pruebas del marcador. Sin base de datos: se le da una tabla falsa.

Lo que se comprueba es que el marcador NO afirme cosas que sus datos no
sostienen. Es la prueba más importante del proyecto, porque el fallo que
arregla no era un error de cálculo: era una tabla bien formateada que
invitaba a leer un ganador donde no había ninguno.

    python pruebas/marcador.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import resolver

fallos = []


def check(nombre, cond, detalle=""):
    print(f"  {'OK  ' if cond else 'FALLA'}  {nombre}{'   ' + detalle if detalle else ''}")
    if not cond:
        fallos.append(nombre)


# ---------------------------------------------------------------------------
class TablaFalsa:
    """Imita el encadenamiento de supabase-py lo justo para `_leer_resueltas`."""

    def __init__(self, filas):
        self._filas = filas

    def table(self, _):        return self
    def select(self, *a, **k): return self
    def order(self, *a, **k):  return self
    def eq(self, *a, **k):     return self

    @property
    def not_(self):            return self
    def is_(self, *a, **k):    return self

    def range(self, a, b):
        self._trozo = self._filas[a:b + 1]
        return self

    def execute(self):
        return type("R", (), {"data": self._trozo})()


def prediccion(metodo, ticker, fecha, prob, cayo, acertada=True):
    return {"metodo": metodo, "ticker": ticker, "emitida_en": fecha,
            "horizonte_d": 5, "prob_caida": prob, "umbral_caida": 0.03,
            "cayo": cayo, "acertada": acertada, "regimen_resol": "normal"}


METODOS = ["baseline_naive", "baseline_tendencia", "llm_ajustado", "regla_vix"]
TICKERS = [f"T{i:02d}" for i in range(18)]


def capturar(filas):
    """Ejecuta el marcador y devuelve lo que imprimió."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        resolver.marcador(TablaFalsa(filas))
    return buf.getvalue()


# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print("CASO 1 — la semana real del 5-sep: dos tandas duplicadas, casi 0 caídas")
print("=" * 74)

filas = []
for fecha in ("2026-08-28", "2026-08-29"):        # el duplicado
    for t in TICKERS:
        cayo = (t == "T07")                        # una sola caída
        for m in METODOS:
            p = 0.084 if m == "regla_vix" else 0.15
            filas.append(prediccion(m, t, fecha, p, cayo, acertada=not cayo))

salida = capturar(filas)
print(salida)

check("no imprime ranking", "SIN RANKING" in salida)
check("cuenta filas y apuestas por separado",
      "144 filas" in salida and "36 apuestas distintas" in salida)
# Ojo con buscar "elevac" a secas: el pie de página dice "elevación" y da
# un falso positivo. Lo que no debe existir es la CABECERA del ranking.
cabecera_ranking = [l for l in salida.splitlines()
                    if "elevac" in l and "brier" in l]
check("no se imprime la cabecera del ranking", not cabecera_ranking)
check("no aparece ni un asterisco de Brier", "*" not in salida)
check("sí muestra calibración", "banda 80%" in salida)
check("avisa de por qué el Brier engaña aquí",
      "premia al que declara la probabilidad más baja" in salida)

# El duplicado: 18 tickers reales contados en 36 apuestas. Que el marcador
# enseñe las dos cifras es lo que permite verlo de un vistazo.
check("el duplicado es visible en la propia tabla",
      "36 apuestas distintas" in salida,
      "-> 18 tickers x 2 tandas; con una sola tanda serían 18")


# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print("CASO 2 — con sucesos suficientes: ya se puede ordenar")
print("=" * 74)

import random
random.seed(7)

filas = []
for dia in range(20):
    fecha = f"2026-09-{dia+1:02d}"
    for t in TICKERS:
        cayo = random.random() < 0.15
        for m in METODOS:
            if m == "llm_ajustado":
                # Un método CON señal: sube la probabilidad cuando va a caer.
                p = 0.45 + random.random() * 0.2 if cayo else 0.05 + random.random() * 0.15
            elif m == "regla_vix":
                p = 0.084                          # plano: no discrimina
            else:
                p = 0.10 + random.random() * 0.1   # ruido
            filas.append(prediccion(m, t, fecha, round(p, 4), cayo,
                                    acertada=not cayo))

salida = capturar(filas)
print(salida)

check("ahora sí imprime ranking", "SIN RANKING" not in salida and "elevac" in salida)
check("el método con señal encabeza la tabla",
      salida.index("llm_ajustado") < salida.index("baseline_naive"))
check("un método plano no recibe elevación", "—" in salida,
      "-> regla_vix declara siempre 8.4%")
check("la tasa de marcado es la misma para todos",
      salida.count("   72 ") >= 3 or "  72" in salida,
      "-> 20% de 360 = 72 en cada método")


# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print("CASO 3 — sin nada resuelto")
print("=" * 74)
salida = capturar([])
check("lo dice y no inventa tabla",
      "Todavía no hay predicciones resueltas" in salida and "MÉTODO" not in salida)

print("\n" + "=" * 74)
print(f"{len(fallos)} fallos" if fallos else "TODO PASA")
print("=" * 74 + "\n")
sys.exit(1 if fallos else 0)
