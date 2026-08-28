"""
Pruebas de la selección de variante en ingestar_banrep.py.

Sin red: se sustituye `pedir` por respuestas falsas y se comprueba la lógica
de elección, que es donde estuvo el fallo de los ocho meses.

    python pruebas/banrep.py
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ingestar_banrep as ib

fallos = []


def check(nombre, cond, detalle=""):
    print(f"  {'OK  ' if cond else 'FALLA'}  {nombre}{'   ' + detalle if detalle else ''}")
    if not cond:
        fallos.append(nombre)


def serie(fechas):
    obs = "".join(f'<Obs TIME_PERIOD="{f}" OBS_VALUE="1.0"/>' for f in fechas)
    return f'<?xml version="1.0"?><m><DataSet><Series FREQ="D">{obs}</Series></DataSet></m>'


class Resp:
    def __init__(self, contenido):
        self.content = contenido.encode()


def simular(lotes):
    """Devuelve un `pedir` falso que responde una lista de fechas por llamada."""
    llamadas = []

    def falso(url, **kw):
        i = len(llamadas)
        llamadas.append(url)
        return Resp(serie(lotes[i])) if i < len(lotes) else None

    return falso, llamadas


print("\n" + "=" * 74)
print("SELECCIÓN DE VARIANTE — se elige por frescura, no por orden")
print("=" * 74)

hoy = date.today().strftime("%Y%m%d")
original = ib.pedir

# 1. Caso normal: la primera variante ya trae datos recientes. No debe gastar
#    peticiones de más — el arreglo no puede costar seis llamadas por flujo.
ib.pedir, llamadas = simular([["20200101", hoy]])
g, etq = ib.pedir_datos("DF_X", 2010)
check("fresca a la primera: una sola petición", len(llamadas) == 1)
check("y la etiqueta es la sin filtro", etq == "v1.0 sin fechas")

# 2. EL CASO REAL del 2026-08-28. La variante sin fechas llega a hoy; la que
#    lleva `endPeriod` corta en diciembre. Antes ganaba la segunda por ir
#    primera en la lista, y así se perdieron ocho meses de CBR.
ib.pedir, llamadas = simular([["19980213", "20260828"], ["20100101", "20251231"]])
g, etq = ib.pedir_datos("DF_CBR_DAILY_HIST", 2010)
check("CBR real: se queda con la serie completa",
      ib._fecha_maxima(g) == "2026-08-28", f"-> {ib._fecha_maxima(g)}")

# 3. Si TODAS vienen rezagadas, gana la menos mala y se dice cuál es.
ib.pedir, llamadas = simular([["20200101", "20251231"],
                              ["20200101", "20260115"],
                              ["20200101", "20240101"]])
g, etq = ib.pedir_datos("DF_X", 2010)
check("todas rezagadas: elige la más fresca", "2026-01-15" in etq, f"-> {etq}")
check("y lo dice en la etiqueta", "menos rezagada" in etq)

# 4. Nadie responde: no debe lanzar, debe devolver vacío.
ib.pedir = lambda url, **kw: None
g, etq = ib.pedir_datos("DF_X", 2010)
check("nadie responde: vacío sin excepción", g == {} and etq == "")

# 5. Respuesta con XML válido pero sin observaciones: tampoco debe elegirse.
ib.pedir, _ = simular([[], ["20200101", hoy]])
g, etq = ib.pedir_datos("DF_X", 2010)
check("una respuesta vacía no gana la carrera",
      ib._fecha_maxima(g) == date.today().isoformat(), f"-> {ib._fecha_maxima(g)}")

# 6. El umbral: 45 días. Justo por debajo pasa, justo por encima no.
from datetime import timedelta
casi = (date.today() - timedelta(days=40)).strftime("%Y%m%d")
ib.pedir, llamadas = simular([["20200101", casi]])
g, etq = ib.pedir_datos("DF_X", 2010)
check("40 días de rezago se aceptan (festivos, demoras)", len(llamadas) == 1)

tarde = (date.today() - timedelta(days=60)).strftime("%Y%m%d")
ib.pedir, llamadas = simular([["20200101", tarde], ["20200101", hoy]])
g, etq = ib.pedir_datos("DF_X", 2010)
check("60 días no: sigue buscando y encuentra la buena",
      len(llamadas) == 2 and ib._fecha_maxima(g) == date.today().isoformat())

ib.pedir = original

print("\n" + "=" * 74)
print(f"{len(fallos)} fallos" if fallos else "TODO PASA")
print("=" * 74 + "\n")
sys.exit(1 if fallos else 0)
