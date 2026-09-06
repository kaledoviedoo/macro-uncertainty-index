"""
comun.py (base compartida por todos los scripts)

Concentra lo que de otro modo estaría copiado en cada ingestor: la conexión a
Supabase, la lectura paginada, la escritura idempotente y el registro de
fallos. Los scripts importan de aquí y se quedan solo con su lógica propia.

Cómo funciona:

  · `conectar()` usa la clave de servicio (escribe). `conectar_lectura()` usa
    la pública, que solo lee por política RLS, y es la de la app Dash.
  · `cargar_entorno()` lee el .env si existe y si no confía en las variables
    de entorno, que es lo que permite correr igual en local y en CI.
  · `leer_todo()` pagina. PostgREST corta en 1.000 filas sin avisar, y un
    `.limit()` mayor no sirve porque el tope lo impone el servidor.
  · `upsert()` escribe por lotes con `on conflict`, así que repetir una
    ingesta diaria que se solapa con lo ya guardado es inofensivo.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from supabase import Client, create_client

RAIZ = Path(__file__).resolve().parent.parent

# Activos sobre los que se predice. Las tasas y el VIX entran como variables
# explicativas, no como objetivos (una "caída del 3 %" en el VIX no describe
# ningún evento). Vive aquí porque lo usan el emisor y el extractor.
TIPOS_OBJETIVO = ("accion", "indice", "etf", "materia_prima", "divisa")

CONTACTO = "kaledoviedoo@gmail.com"

# Ninguna cabecera sirve para todas las fuentes: las agencias estadísticas de
# EE.UU. quieren un identificador con contacto y los WAF comerciales solo
# hablan con navegadores. `verificar.py` prueba las tres y guarda la que
# funcionó en `fuentes.ua_perfil`.
PERFILES_UA: dict[str, dict[str, str]] = {
    "contacto": {
        "User-Agent": f"MotorCausal/0.1 (+{CONTACTO})",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    },
    "navegador": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "application/rss+xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Connection": "keep-alive",
    },
    "minimo": {
        "User-Agent": "python-requests/2.32",
    },
}

PAGINA = 1000


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------
def _describir_clave(clave: str) -> str | None:
    """Rol que declara la clave, deducido sin llamar a la API."""
    if clave.startswith("sb_secret_"):
        return "service_role"
    if clave.startswith("sb_publishable_"):
        return "anon"
    if clave.startswith("eyJ"):
        try:
            p = clave.split(".")[1]
            p += "=" * (-len(p) % 4)
            return json.loads(base64.urlsafe_b64decode(p)).get("role")
        except Exception:
            return None
    return None


def cargar_entorno() -> None:
    """Lee el .env si existe; si no, manda el entorno (así corre en CI)."""
    env = RAIZ / ".env"
    if env.exists():
        load_dotenv(env, override=True)


def _falta(nombre: str) -> str:
    origen = "el .env" if (RAIZ / ".env").exists() else "las variables de entorno"
    return (f"Falta {nombre} en {origen}.\n"
            f"  Local:  añádela a {RAIZ / '.env'}\n"
            f"  CI:     defínela como secret del repositorio")


def _limpiar(valor: str | None) -> str:
    return (valor or "").strip().strip('"').strip("'")


def conectar(silencioso: bool = False) -> Client:
    """Conexión con la clave de servicio. Escribe y salta RLS."""
    cargar_entorno()
    url = _limpiar(os.getenv("SUPABASE_URL"))
    clave = _limpiar(os.getenv("SUPABASE_SERVICE_KEY"))

    if not url:
        sys.exit(_falta("SUPABASE_URL"))
    if not clave or clave.startswith("pega_aqui"):
        sys.exit(_falta("SUPABASE_SERVICE_KEY"))

    rol = _describir_clave(clave)
    if rol == "anon":
        sys.exit("Esa es la clave pública: solo lee. Los ingestores escriben.\n"
                 "Usa la service_role o la sb_secret.")

    sb = create_client(url, clave)
    try:
        sb.table("activos").select("ticker").limit(1).execute()
    except Exception as exc:
        if "Invalid API key" in str(exc):
            sys.exit("Supabase rechaza la clave. "
                     "Revísala en Project Settings > API Keys.")
        raise

    if not silencioso:
        print(f"Conectado a {url}  (rol: {rol})")
    return sb


def conectar_lectura() -> Client:
    """Conexión con la clave pública. Solo lee, y es la que usa la app Dash."""
    cargar_entorno()
    url = _limpiar(os.getenv("SUPABASE_URL"))
    clave = _limpiar(os.getenv("SUPABASE_ANON_KEY"))
    if not url:
        sys.exit(_falta("SUPABASE_URL"))
    if not clave:
        sys.exit(_falta("SUPABASE_ANON_KEY"))
    return create_client(url, clave)


# ---------------------------------------------------------------------------
# Lectura y escritura
# ---------------------------------------------------------------------------
def leer_todo(sb: Client, tabla: str, columnas: str, *,
              filtros: dict[str, Any] | None = None,
              orden: str | None = None,
              pagina: int = PAGINA) -> list[dict]:
    """Lee una tabla entera paginando (PostgREST corta en 1.000 sin avisar)."""
    filas: list[dict] = []
    desde = 0
    while True:
        q = sb.table(tabla).select(columnas)
        for col, val in (filtros or {}).items():
            q = q.eq(col, val)
        if orden:
            q = q.order(orden)
        trozo = q.range(desde, desde + pagina - 1).execute().data
        filas.extend(trozo)
        if len(trozo) < pagina:
            return filas
        desde += pagina


def lotes(items: list[Any], tam: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), tam):
        yield items[i:i + tam]


def upsert(sb: Client, tabla: str, filas: list[dict], conflicto: str,
           tam_lote: int = 500) -> int:
    """Escritura idempotente por lotes."""
    escritas = 0
    for lote in lotes(filas, tam_lote):
        sb.table(tabla).upsert(lote, on_conflict=conflicto).execute()
        escritas += len(lote)
    return escritas


def log(sb: Client, proceso: str, ticker: str | None, exito: bool,
        filas: int | None = None, error: str | None = None) -> None:
    """Deja constancia del fallo. Un error silencioso es un error que vuelve."""
    try:
        sb.table("ingesta_log").insert({
            "proceso": proceso,
            "ticker": ticker,
            "exito": exito,
            "filas": filas,
            "error": (error or "")[:500] or None,
        }).execute()
    except Exception as exc:
        print(f"  (no se pudo escribir en ingesta_log: {exc})")


def limpiar_para_postgres(texto: str) -> str:
    """
    Quita los bytes nulos y sustitutos sueltos que PostgreSQL rechaza con el
    error 22P05. Aparecen al extraer texto de webs (el Banco de Japón, sobre
    todo) y rompen la escritura antes incluso de llegar a la base.
    """
    if not texto:
        return texto
    limpio = texto.replace("\x00", "").replace("\\u0000", "")
    return "".join(c for c in limpio if not 0xD800 <= ord(c) <= 0xDFFF)
