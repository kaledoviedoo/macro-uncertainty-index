"""
Prueba de los dos arreglos, con los datos REALES del run del 2026-08-28.
No toca la base: solo ejercita las funciones nuevas.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from modelos import (ABANICO_DISPERSION, MIN_CUERPO_PARA_VARIOS,
                     detectar_abanico, validar_por_partes)

fallos = []


def check(nombre, cond, detalle=""):
    print(f"  {'OK  ' if cond else 'FALLA'}  {nombre}{'   ' + detalle if detalle else ''}")
    if not cond:
        fallos.append(nombre)


print("\n" + "=" * 74)
print("ARREGLO 1 — un campo malo ya no tumba el documento")
print("=" * 74)

# Reconstrucción del caso Jackson Hole: tres impactos, el tercero con un
# canal que no existe en el enum. Antes se perdían los tres.
bruto = {
    "resumen": "Warsh calma los nervios en Jackson Hole",
    "es_relevante": True,
    "impactos": [
        {"ticker": "^GSPC", "canal": "tasa_interes", "horizonte_d": 10,
         "factor_incert": 1.25, "cola": "izquierda", "intensidad_cola": 0.4,
         "cita": "Warsh said the committee would remain patient on rates",
         "confianza": 0.85, "razonamiento": "Tono de la Fed sobre tipos."},
        {"ticker": "^TNX", "canal": "tasa_interes", "horizonte_d": 20,
         "factor_incert": 1.40, "cola": "derecha", "intensidad_cola": 0.5,
         "cita": "Warsh said the committee would remain patient on rates",
         "confianza": 0.80, "razonamiento": "Expectativas de tipos largos."},
        # El culpable: canal inventado.
        {"ticker": "DX-Y.NYB", "canal": "politica_monetaria", "horizonte_d": 15,
         "factor_incert": 1.20, "cola": "ninguna", "intensidad_cola": 0.0,
         "cita": "Warsh said the committee would remain patient on rates",
         "confianza": 0.70, "razonamiento": "Diferencial de tipos."},
    ],
}

ext, rechazos = validar_por_partes(bruto)
check("sobreviven los 2 impactos sanos", len(ext.impactos) == 2,
      f"-> {[i.ticker for i in ext.impactos]}")
check("se rechaza solo el malo", len(rechazos) == 1)
check("el motivo nombra el valor culpable",
      "politica_monetaria" in rechazos[0], f"-> {rechazos[0][:88]}")

# Un sinónimo que SÍ está en el mapa de alias debe pasar, no rechazarse.
bruto_ingles = {"impactos": [dict(bruto["impactos"][0], canal="interest_rate")]}
ext2, r2 = validar_por_partes(bruto_ingles)
check("'interest_rate' se traduce y pasa", len(ext2.impactos) == 1 and not r2)

# Basura total no debe lanzar excepción.
ext3, r3 = validar_por_partes({"impactos": ["no soy un dict", 42]})
check("basura no lanza excepción", ext3.impactos == [] and r3 == [])

ext4, r4 = validar_por_partes({})
check("dict vacío no lanza excepción", ext4.impactos == [])


print("\n" + "=" * 74)
print("ARREGLO 2 — el abanico del FT ahora se detecta")
print("=" * 74)

# Los cinco impactos literales del run: doc 13, bonos convertibles.
abanico_ft = [
    {"ticker": "NVDA",  "canal": "demanda", "cola": "ninguna", "factor_incert": 1.15},
    {"ticker": "AAPL",  "canal": "demanda", "cola": "ninguna", "factor_incert": 1.12},
    {"ticker": "MSFT",  "canal": "demanda", "cola": "ninguna", "factor_incert": 1.18},
    {"ticker": "^NDX",  "canal": "demanda", "cola": "ninguna", "factor_incert": 1.14},
    {"ticker": "^GSPC", "canal": "demanda", "cola": "ninguna", "factor_incert": 1.13},
]
es, det = detectar_abanico(abanico_ft)
check("el caso real se detecta", es, f"-> {det}")

# El detector VIEJO exigía valores idénticos. Se comprueba que ese criterio
# no habría saltado, que es exactamente por lo que el abanico pasó entero.
firmas = {(f["factor_incert"], f["cola"]) for f in abanico_ft}
check("el detector viejo NO lo habría visto", len(firmas) != 1,
      f"-> {len(firmas)} firmas distintas, exigía 1")

# Un evento macro real: mismo canal, pero exposiciones distintas. Debe pasar.
fomc = [
    {"ticker": "^GSPC", "canal": "tasa_interes", "cola": "izquierda", "factor_incert": 1.20},
    {"ticker": "^TNX",  "canal": "tasa_interes", "cola": "izquierda", "factor_incert": 1.65},
    {"ticker": "GC=F",  "canal": "tasa_interes", "cola": "izquierda", "factor_incert": 1.35},
]
es_f, _ = detectar_abanico(fomc)
check("un FOMC real NO se marca (factores dispersos)", not es_f,
      f"-> dispersión 0.45 >= {ABANICO_DISPERSION}")

# Colas distintas: hay juicio por activo, no es relleno.
mixto = [dict(f) for f in abanico_ft[:3]]
mixto[0]["cola"] = "derecha"
check("colas distintas NO se marcan", not detectar_abanico(mixto)[0])

check("dos impactos nunca son abanico", not detectar_abanico(abanico_ft[:2])[0])


print("\n" + "=" * 74)
print("ARREGLO 2b — el titular estirado")
print("=" * 74)

titular = "Zero-interest convertible bonds set for record year"
doc = titular + "\n"
check("el documento del FT es un titular", len(doc) < MIN_CUERPO_PARA_VARIOS,
      f"-> {len(doc)} car. < {MIN_CUERPO_PARA_VARIOS}")
check("la cita mínima cabe en el titular", len(titular) > 20,
      "-> la regla de la cita se cumple sola: no garantiza nada aquí")

print("\n" + "=" * 74)
print(f"{len(fallos)} fallos" if fallos else "TODO PASA")
print("=" * 74 + "\n")
sys.exit(1 if fallos else 0)
