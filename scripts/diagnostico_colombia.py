"""
diagnostico_colombia.py — De dónde sacar CBR e IBR de 2026.

NO ESCRIBE NADA. Solo pregunta y enseña lo que le contestan.

EL PROBLEMA

`ingestar_banrep.py` deja un hueco de ocho meses en CBR y en IBR. La causa no
es un fallo del código: el SDMX del Banco de la República parte cada serie en
dos, `_HIST` cierra en el último año COMPLETO y `_LATEST` devuelve UNA sola
observación, la de hoy. El año en curso no lo cubre ninguno de los dos.

Se ve en la propia salida del ingestor del 2026-08-28:

    DF_CBR_DAILY_HIST:    5.844 observaciones      -> hasta 2025-12-31
    DF_CBR_DAILY_LATEST:      1 observación        -> hoy
    serie elegida:        5.845 obs   AVISO: hueco de 240 días

Cada corrida diaria añade un punto suelto. De enero a agosto de 2026 no hay
nada, y por esta vía no lo habrá nunca.

QUÉ COMPRUEBA ESTE ARCHIVO

  1. Si el catálogo SDMX tiene algún otro flujo con datos de 2026 que el
     ingestor no esté probando. Hoy solo prueba cuatro nombres derivados del
     que está configurado; el catálogo tiene cientos.

  2. Si el dataset de IBR en datos.gov.co (ev8i-uzwt) sirve de sustituto:
     qué campos trae, hasta qué fecha llega y qué plazos mezcla. Es el mismo
     camino que ya arregló la TRM, que está al día y sin huecos.

  3. Cómo está el hueco en TU base ahora mismo, para saber qué hay que
     rellenar exactamente y no rellenar de más.

POR QUÉ TIENES QUE CORRERLO TÚ

Ni el contenedor donde trabajo ni la VM local del puente pueden salir a
`totoro.banrep.gov.co` ni a `datos.gov.co`: las dos redes están restringidas.
Tu Windows sí llega, y el runner de GitHub también. Por eso el ingestor
funciona ahí y no aquí.

    python scripts/diagnostico_colombia.py
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from comun import CONTACTO, PERFILES_UA, conectar, leer_todo

BASE_SDMX = "https://totoro.banrep.gov.co/nsi-jax-ws/rest"
BASE_SOCRATA = "https://www.datos.gov.co/resource"
AGENCIA = "ESTAT"
IBR_SOCRATA = "ev8i-uzwt"

# Lo que buscamos en los nombres del catálogo. CBR es la tasa de política;
# IBR la interbancaria; TIB y TPM aparecen en nombres antiguos de lo mismo.
PISTAS = ("CBR", "IBR", "TIB", "TPM", "POLIT", "INTERV", "TASA")


def linea(t: str = "") -> None:
    print(f"\n{'=' * 74}")
    if t:
        print(t)
        print("=" * 74)


def pedir(url: str, timeout: int = 90) -> requests.Response | None:
    """Prueba los tres perfiles de User-Agent, como hace el ingestor."""
    for perfil, h in PERFILES_UA.items():
        try:
            r = requests.get(url, headers=h, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception:
            pass
    return None


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------
def catalogo_sdmx() -> list[tuple[str, str]]:
    linea("1. CATÁLOGO SDMX — ¿hay algún flujo que no estemos probando?")
    r = pedir(f"{BASE_SDMX}/dataflow/{AGENCIA}/all/latest")
    if r is None:
        print("  Sin respuesta del catálogo. ¿Tienes red hacia banrep?")
        return []

    try:
        raiz = ET.fromstring(r.content)
    except ET.ParseError as exc:
        print(f"  Respuesta no parseable: {exc}")
        return []

    flujos = []
    for el in raiz.iter():
        if local(el.tag) != "Dataflow" or not el.get("id"):
            continue
        nombre = next((h.text.strip() for h in el.iter()
                       if local(h.tag) == "Name" and (h.text or "").strip()), "")
        flujos.append((el.get("id"), nombre))

    print(f"  {len(flujos)} flujos en el catálogo.")
    interesa = sorted({f for f in flujos
                       if any(p in f[0].upper() for p in PISTAS)})
    print(f"  {len(interesa)} mencionan alguna de nuestras pistas:\n")
    for fid, nombre in interesa:
        print(f"    {fid:<34} {nombre[:56]}")
    return interesa


def cobertura(flujo: str) -> None:
    """¿Hasta qué fecha llega este flujo? Es la única pregunta que importa."""
    cola = "dimensionAtObservation=TIME_PERIOD&detail=full"
    variantes = [
        ("2026 explícito", f"{AGENCIA},{flujo},1.0/all/ALL/?startPeriod=2026&endPeriod=2026&{cola}"),
        ("v1.0 sin fechas", f"{AGENCIA},{flujo},1.0/all/ALL/?{cola}"),
        ("sin version",     f"{AGENCIA},{flujo}/all/ALL/?{cola}"),
    ]

    for etiqueta, ruta in variantes:
        r = pedir(f"{BASE_SDMX}/data/{ruta}", timeout=60)
        if r is None:
            continue
        try:
            raiz = ET.fromstring(r.content)
        except ET.ParseError:
            continue

        periodos = []
        for el in raiz.iter():
            if local(el.tag) != "Obs":
                continue
            p = el.get("TIME_PERIOD")
            if p is None:
                p = next((h.get("value") for h in el
                          if local(h.tag) == "ObsDimension"), None)
            if p:
                periodos.append(p)

        if not periodos:
            print(f"    {flujo:<32} [{etiqueta}] responde pero sin observaciones")
            continue

        de2026 = [p for p in periodos if p.startswith("2026")]
        marca = f"  <<< {len(de2026)} obs de 2026" if de2026 else ""
        print(f"    {flujo:<32} [{etiqueta}] {len(periodos):>6,} obs   "
              f"{min(periodos)} a {max(periodos)}{marca}")
        return

    print(f"    {flujo:<32} sin respuesta con ninguna variante")


# ---------------------------------------------------------------------------
def socrata_ibr() -> None:
    linea("2. IBR EN DATOS.GOV.CO — ¿sirve como sustituto?")
    cab = {"User-Agent": f"MotorCausal/0.1 (+{CONTACTO})",
           "Accept": "application/json"}
    url = f"{BASE_SOCRATA}/{IBR_SOCRATA}.json"

    try:
        r = requests.get(url, headers=cab, params={"$limit": 1}, timeout=60)
    except Exception as exc:
        print(f"  {type(exc).__name__}: {exc}")
        return
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:200]}")
        return

    muestra = r.json()
    if not muestra:
        print("  El dataset está vacío.")
        return

    campos = list(muestra[0].keys())
    print(f"  Dataset {IBR_SOCRATA}")
    print(f"  Campos: {', '.join(campos)}\n")
    print("  Una fila de ejemplo:")
    for k, v in muestra[0].items():
        print(f"    {k:<28} {str(v)[:52]}")

    # El campo de fecha, para preguntar por el rango.
    f_fecha = next((c for c in campos
                    if any(k in c.lower() for k in ("fecha", "vigencia", "date"))), None)
    if not f_fecha:
        print("\n  No encuentro un campo de fecha. Hay que mirarlo a mano.")
        return

    try:
        r = requests.get(url, headers=cab, timeout=60, params={
            "$select": f"min({f_fecha}) as desde, max({f_fecha}) as hasta, count(*) as filas"})
        print(f"\n  Rango completo: {r.json()[0]}")

        r = requests.get(url, headers=cab, timeout=60, params={
            "$select": "count(*) as filas_2026",
            "$where": f"{f_fecha} >= '2026-01-01'"})
        n2026 = r.json()[0].get("filas_2026", "?")
        print(f"  Filas de 2026:  {n2026}")
        if str(n2026) in ("0", "?"):
            print("  >>> NO sirve: tiene el mismo hueco que el SDMX.")
        else:
            print("  >>> SÍ cubre 2026. Es el sustituto.")
    except Exception as exc:
        print(f"  Al consultar el rango: {type(exc).__name__}: {exc}")

    # Un dataset de IBR mezcla plazos (overnight, un mes, tres meses). Si no
    # se filtra, varios plazos colisionan en la clave (ticker, fecha) y el
    # upsert guarda uno al azar cada noche. Es el mismo fallo que ya obligó a
    # agrupar por serie en el SDMX.
    for c in campos:
        if any(k in c.lower() for k in ("plazo", "tipo", "descripcion", "serie")):
            try:
                r = requests.get(url, headers=cab, timeout=60, params={
                    "$select": f"{c}, count(*) as n", "$group": c, "$limit": 20})
                print(f"\n  Valores distintos de «{c}» — hay que elegir UNO:")
                for fila in r.json():
                    print(f"    {str(fila.get(c))[:44]:<46} {fila.get('n')}")
            except Exception:
                pass


# ---------------------------------------------------------------------------
def hueco_en_la_base() -> None:
    linea("3. EL HUECO EN TU BASE — qué hay que rellenar exactamente")
    sb = conectar(silencioso=True)

    for t in ("CBR", "IBR"):
        filas = leer_todo(sb, "precios_diarios", "fecha,cierre",
                          filtros={"ticker": t}, orden="fecha")
        if not filas:
            print(f"\n  {t}: sin datos.")
            continue

        fechas = [date.fromisoformat(f["fecha"]) for f in filas]
        huecos = [(a, b, (b - a).days) for a, b in zip(fechas, fechas[1:])
                  if (b - a).days > 10]

        print(f"\n  {t}: {len(filas):,} filas   {fechas[0]} a {fechas[-1]}")
        print(f"      último valor: {filas[-1]['cierre']}")
        if not huecos:
            print("      sin huecos mayores de 10 días")
            continue
        for a, b, d in huecos[-3:]:
            print(f"      HUECO  {a} → {b}   ({d} días)")

        # Cuántos días hábiles faltan de verdad. Es el tamaño real del
        # trabajo: si son 160 días hábiles, hay que rellenarlos; si el hueco
        # cae en festivos y fines de semana, no falta nada.
        peor = max(huecos, key=lambda h: h[2])
        habiles = sum(1 for n in range((peor[1] - peor[0]).days)
                      if (peor[0].toordinal() + n) % 7 not in (5, 6))
        print(f"      ~{habiles} días hábiles dentro del mayor hueco")


# ---------------------------------------------------------------------------
def main() -> None:
    print("\n  DIAGNÓSTICO DE LAS SERIES COLOMBIANAS — no escribe nada\n")

    interesa = catalogo_sdmx()
    if interesa:
        linea("1b. COBERTURA DE CADA CANDIDATO — ¿alguno llega a 2026?")
        print("  (una petición por flujo; tarda)\n")
        for fid, _ in interesa:
            cobertura(fid)

    socrata_ibr()

    try:
        hueco_en_la_base()
    except Exception as exc:
        print(f"\n  No pude consultar la base: {type(exc).__name__}: {exc}")

    linea()
    print("""  Pégame esta salida entera.

  Con el punto 1 sabré si el hueco se arregla dentro del propio SDMX
  —sería lo más limpio: un nombre de flujo distinto en `activos`— o si
  hay que cambiar de fuente.

  Con el punto 2 sabré si IBR se resuelve como se resolvió la TRM:
  moviéndolo a datos.gov.co, que ya funciona y está al día.

  CBR es un caso aparte y más fácil de lo que parece. La tasa de política
  NO es una serie diaria: es una ESCALERA. Solo cambia cuando la Junta lo
  decide, ocho veces al año, y entre decisión y decisión vale lo mismo
  todos los días. Rellenar ese hueco no es estimar nada, es copiar el
  último valor anunciado hasta el siguiente anuncio. En 2026 la Junta la
  subió de 9,25 % (diciembre 2025) a 12 % (julio 2026), así que son tres
  o cuatro escalones, no 160 datos que buscar.""")


if __name__ == "__main__":
    main()
