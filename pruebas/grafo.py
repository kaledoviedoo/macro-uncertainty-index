"""
Pruebas de sinapsis.py. Sin base de datos: se le da una tabla falsa.

Lo que se comprueba no es que imprima bonito, sino que la ARITMÉTICA que
enseña sea la misma que el motor usa. Un inspector que muestre un número
distinto del que se aplica es peor que no tener inspector: da confianza
sobre algo que no está pasando.

    python pruebas/grafo.py
"""

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sinapsis

fallos = []


def check(nombre, cond, detalle=""):
    print(f"  {'OK  ' if cond else 'FALLA'}  {nombre}{'   ' + detalle if detalle else ''}")
    if not cond:
        fallos.append(nombre)


# ---------------------------------------------------------------------------
# Los seis impactos del petróleo del 2026-08-28, que dieron factor 1.66.
IMPACTOS = [
    dict(ticker="CL=F", canal="oferta", horizonte_d=20, factor_incert=1.25,
         cola="izquierda", intensidad_cola=0.4, confianza=0.90, noticia_id=1,
         cita="U.S. uranium production more than tripled in 2025",
         razonamiento="Más oferta energética presiona el crudo a la baja.",
         lote_uniforme=False, long_documento=1917, cita_verificada=True,
         creado_en="2026-08-28T00:00:00Z"),
    dict(ticker="CL=F", canal="demanda", horizonte_d=15, factor_incert=1.20,
         cola="ninguna", intensidad_cola=0.0, confianza=0.70, noticia_id=2,
         cita="Eight petroleum liquids pipeline projects have been completed",
         razonamiento="Nueva capacidad de transporte altera los diferenciales.",
         lote_uniforme=False, long_documento=3927, cita_verificada=True,
         creado_en="2026-08-28T00:00:00Z"),
    dict(ticker="CL=F", canal="oferta", horizonte_d=10, factor_incert=1.30,
         cola="izquierda", intensidad_cola=0.3, confianza=0.80, noticia_id=3,
         cita="Dangote refinery drives increase in petroleum shipments",
         razonamiento="La refinería añade oferta de producto refinado.",
         lote_uniforme=False, long_documento=2375, cita_verificada=True,
         creado_en="2026-08-28T00:00:00Z"),
    # Uno apartado: no debe entrar en la suma.
    dict(ticker="CL=F", canal="demanda", horizonte_d=30, factor_incert=1.15,
         cola="ninguna", intensidad_cola=0.0, confianza=0.85, noticia_id=4,
         cita="Zero-interest convertible bonds set for record year",
         razonamiento="Titular estirado.", lote_uniforme=True,
         long_documento=52, cita_verificada=True, creado_en="2026-08-28T00:00:00Z"),
    # Uno con horizonte vencido: tampoco.
    dict(ticker="CL=F", canal="costes", horizonte_d=2, factor_incert=1.90,
         cola="izquierda", intensidad_cola=0.6, confianza=0.95, noticia_id=5,
         cita="Cita de un impacto ya vencido que no debe contar",
         razonamiento="Vencido.", lote_uniforme=False, long_documento=900,
         cita_verificada=True, creado_en="2026-08-20T00:00:00Z"),
    # Uno sin cita verificada: nunca.
    dict(ticker="CL=F", canal="oferta", horizonte_d=20, factor_incert=2.40,
         cola="izquierda", intensidad_cola=0.9, confianza=0.99, noticia_id=6,
         cita="Cita no verificada que no debe aparecer jamas",
         razonamiento="No verificado.", lote_uniforme=False, long_documento=900,
         cita_verificada=False, creado_en="2026-08-28T00:00:00Z"),
]

TITULARES = {
    1: "U.S. uranium production more than tripled in 2025",
    2: "Eight petroleum liquids pipeline projects completed",
    3: "Dangote refinery drives increase in petroleum shipments",
    4: "Zero-interest convertible bonds set for record year",   # el abanico
    5: "Noticia de un impacto ya vencido",
    6: "Noticia de un impacto sin cita verificada",
}
NOTICIAS = [{"id": i, "titular": t, "publicado_en": "2026-08-28",
             "fuente_id": 1} for i, t in TITULARES.items()]
FUENTES = [{"id": 1, "nombre": "EIA - Today in Energy", "nivel_confianza": 1}]
ACTIVOS = [{"ticker": "CL=F", "nombre": "Petroleo WTI (futuros)",
            "tipo": "materia_prima"}]


class TablaFalsa:
    """Imita el encadenamiento de supabase-py para leer_todo y ajuste_llm."""

    def __init__(self):
        self._datos, self._filtros, self._limite = [], {}, None

    def table(self, nombre):
        self._filtros, self._limite = {}, None
        self._datos = {"impactos": IMPACTOS, "noticias": NOTICIAS,
                       "fuentes": FUENTES, "activos": ACTIVOS}.get(nombre, [])
        return self

    def select(self, *a, **k):  return self
    def order(self, *a, **k):   return self
    def limit(self, n):         self._limite = n; return self

    def eq(self, col, val):
        self._filtros[col] = val
        return self

    def gte(self, col, val):
        self._datos = [d for d in self._datos if d.get(col, 0) >= val]
        return self

    def or_(self, expr):
        # `lote_uniforme.is.null,lote_uniforme.eq.false`
        self._datos = [d for d in self._datos if not d.get("lote_uniforme")]
        return self

    def range(self, a, b):
        self._trozo = self._aplicar()[a:b + 1]
        return self

    def _aplicar(self):
        d = self._datos
        for col, val in self._filtros.items():
            d = [x for x in d if x.get(col) == val]
        return d

    def execute(self):
        datos = getattr(self, "_trozo", None)
        if datos is None:
            datos = self._aplicar()
            if self._limite:
                datos = datos[:self._limite]
        self._trozo = None
        return type("R", (), {"data": datos})()


def correr(*args):
    sys.argv = ["sinapsis.py", *args]
    buf = io.StringIO()
    with redirect_stdout(buf):
        sinapsis.conectar = lambda **k: TablaFalsa()
        sinapsis.main()
    return buf.getvalue()


# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
print("SINAPSIS — el factor que enseña es el factor que se usa")
print("=" * 74)

salida = correr("--ticker", "CL=F")
print(salida)

# Los tres vigentes, agrupados por canal como los compone el motor:
#   oferta   ->  0.90·(1.25²−1) = 0.506   y   0.80·(1.30²−1) = 0.552
#   demanda  ->  0.70·(1.20²−1) = 0.308
# Dentro de `oferta` el mayor cuenta entero y el otro al PESO_CORRELADO.
from predecir import PESO_CORRELADO

of = sorted([0.90 * (1.25**2 - 1), 0.80 * (1.30**2 - 1)], reverse=True)
esperado = of[0] + PESO_CORRELADO * of[1] + 0.70 * (1.20**2 - 1)
raiz = (1 + esperado) ** 0.5

check("el exceso lleva el descuento por canal", f"{esperado:.3f}" in salida,
      f"-> {esperado:.3f}")
check("la raíz cuadra con el exceso", f"= {raiz:.2f}" in salida, f"-> {raiz:.2f}")
check("el factor mostrado es el de ajuste_llm", f"factor {raiz:.2f}" in salida)

# Lo que este bloque vigila de verdad: que el inspector y el motor no tengan
# dos aritméticas distintas. Ese fallo ya ocurrió una vez —sinapsis sumaba
# plano y culpaba al tope de 2,50 de una bajada que causaba el descuento— y
# ahora el propio script lo grita si vuelve a pasar.
check("no avisa de divergencia con el motor",
      "AVISO: el motor aplica" not in salida)
check("no inventa un recorte por el tope", "tope de 2,50" not in salida,
      f"-> {raiz:.2f} no está cerca de 2,50")
check("enseña la composición por canal", "composición por canal" in salida)
check("el canal con dos impactos muestra el descuento",
      f"+ {PESO_CORRELADO}·" in salida)

check("el impacto apartado no aparece", "Zero-interest convertible" not in salida)
check("el de horizonte vencido no aparece", "impacto ya vencido" not in salida)
check("el de cita sin verificar no aparece", "no verificada" not in salida)
check("dice cuántos apartó", "1 impactos apartados" in salida
      or "1 apartados" in salida)

check("muestra la cita literal", "U.S. uranium production more than tripled" in salida)
check("muestra el razonamiento", "presiona el crudo a la baja" in salida)
check("muestra la fuente y su nivel", "EIA - Today in Energy" in salida and "N1" in salida)

# Los aportes individuales deben sumar el total: es lo que hace auditable
# el número. Si no cuadran, la descomposición es decorativa.
import re
aportes = [float(x) for x in re.findall(r"aporta (\d+\.\d{3})", salida)]
bruto = sum(a for a, *_ in [(x,) for x in aportes])

# Los aportes individuales son EN BRUTO, antes del descuento. Su suma NO
# tiene que dar el exceso — si diera, el descuento no estaría actuando.
check("los aportes brutos suman lo de antes del descuento",
      abs(bruto - (of[0] + of[1] + 0.70 * (1.20**2 - 1))) < 0.002,
      f"-> {bruto:.3f}")
check("y el exceso es MENOR que la suma bruta", esperado < bruto,
      f"-> {esperado:.3f} < {bruto:.3f}")

# Lo que sí debe cuadrar: las subtotales por canal con el exceso total.
subtotales = [float(x) for x in re.findall(r"=  (\d+\.\d{3})$", salida, re.M)]
check("los subtotales por canal suman el exceso",
      abs(sum(subtotales) - esperado) < 0.002,
      f"-> {sum(subtotales):.3f} frente a {esperado:.3f}")

salida_ap = correr("--ticker", "CL=F", "--apartados")
check("--apartados sí los enseña", "Zero-interest convertible" in salida_ap)

print("\n" + "=" * 74)
print(f"{len(fallos)} fallos" if fallos else "TODO PASA")
print("=" * 74 + "\n")
sys.exit(1 if fallos else 0)
