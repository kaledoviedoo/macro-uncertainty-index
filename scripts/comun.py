"""
comun.py — Lo que comparten todos los scripts: conexión, log y escritura.

Vive aparte para que la lógica de cada ingestor sea legible de un vistazo
y para que el diagnóstico de la clave exista en un solo sitio.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

from dotenv import load_dotenv
from supabase import Client, create_client

RAIZ = Path(__file__).resolve().parent.parent

# No hay un User-Agent que sirva para todas las fuentes. Las agencias
# estadísticas de EE.UU. quieren un identificador honesto con contacto;
# los WAF comerciales solo hablan con navegadores. El verificador prueba
# los tres y guarda el ganador en `fuentes.ua_perfil`.
CONTACTO = "kaledoviedoo@gmail.com"

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


# --------------------------------------------------------------------------
def _describir_clave(clave: str) -> str | None:
    """Rol declarado por la clave, si se puede deducir sin llamar a la API."""
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


def conectar(silencioso: bool = False) -> Client:
    env = RAIZ / ".env"
    if not env.exists():
        sys.exit(f"No encuentro el archivo .env en {RAIZ}")
    load_dotenv(env, override=True)

    url = (os.getenv("SUPABASE_URL") or "").strip().strip('"').strip("'")
    clave = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip().strip('"').strip("'")

    if not url:
        sys.exit("Falta SUPABASE_URL en .env")
    if not clave or clave.startswith("pega_aqui"):
        sys.exit("Falta SUPABASE_SERVICE_KEY en .env")

    rol = _describir_clave(clave)
    if rol == "anon":
        sys.exit(
            "Esa es la clave pública: solo lee. Los ingestores escriben.\n"
            "Usa la service_role o la sb_secret."
        )

    sb = create_client(url, clave)
    try:
        sb.table("activos").select("ticker").limit(1).execute()
    except Exception as exc:
        if "Invalid API key" in str(exc):
            sys.exit("Supabase rechaza la clave. Revísala en Project Settings > API Keys.")
        raise

    if not silencioso:
        print(f"Conectado a {url}  (rol: {rol})")
    return sb


# --------------------------------------------------------------------------
def conectar_lectura() -> Client:
    """
    Conexión con la clave PÚBLICA, que solo puede leer (política RLS).

    Es la que usa la app Dash. Si algún día publicas la interfaz o alguien
    mira el proceso, lo peor que puede pasar es que lea datos de mercado.
    La clave de administrador no entra en el frontend.
    """
    env = RAIZ / ".env"
    if not env.exists():
        sys.exit(f"No encuentro el archivo .env en {RAIZ}")
    load_dotenv(env, override=True)

    url = (os.getenv("SUPABASE_URL") or "").strip().strip('"').strip("'")
    clave = (os.getenv("SUPABASE_ANON_KEY") or "").strip().strip('"').strip("'")
    if not url or not clave:
        sys.exit("Faltan SUPABASE_URL o SUPABASE_ANON_KEY en .env")
    return create_client(url, clave)


def log(sb: Client, proceso: str, ticker: str | None, exito: bool,
        filas: int | None = None, error: str | None = None) -> None:
    """Todo fallo queda escrito. Un error silencioso es un error que vuelve."""
    try:
        sb.table("ingesta_log").insert({
            "proceso": proceso,
            "ticker": ticker,
            "exito": exito,
            "filas": filas,
            "error": (error or "")[:500] or None,
        }).execute()
    except Exception as exc:
        # Que falle el log jamás debe tumbar la ingesta.
        print(f"  (no se pudo escribir en ingesta_log: {exc})")


def limpiar_para_postgres(texto: str) -> str:
    """
    Quita lo que PostgreSQL no acepta dentro de una columna `text`.

    El byte nulo es el culpable: Postgres lo rechaza con el error 22P05,
    "unsupported Unicode escape sequence". Aparece al extraer texto de webs
    que traen \\u0000 incrustado, y en el Banco de Japon pasa a menudo.
    Tambien se descartan los sustitutos sueltos, que rompen la codificacion
    a UTF-8 antes incluso de llegar a la base.
    """
    if not texto:
        return texto
    limpio = texto.replace("\x00", "").replace("\\u0000", "")
    return "".join(c for c in limpio if not 0xD800 <= ord(c) <= 0xDFFF)


def lotes(items: list[Any], tam: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), tam):
        yield items[i:i + tam]


# PostgREST corta toda respuesta en 1.000 filas y NO avisa: devuelve mil y
# se queda tan tranquilo. Poner .limit(20000) no sirve de nada — el tope lo
# impone el servidor, no el cliente. Es el fallo silencioso más traicionero
# de Supabase: los datos parecen completos y no lo están.
PAGINA = 1000


def leer_todo(sb: Client, tabla: str, columnas: str, *,
              filtros: dict[str, Any] | None = None,
              orden: str | None = None,
              pagina: int = PAGINA) -> list[dict]:
    """Lee una tabla entera paginando, en vez de confiar en un limit."""
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


def upsert(sb: Client, tabla: str, filas: list[dict], conflicto: str,
           tam_lote: int = 500) -> int:
    """
    Escritura idempotente por lotes.

    Idempotente importa: el ingestor se ejecuta a diario y siempre pide una
    ventana que se solapa con lo ya guardado. Sin `on conflict` tendrías
    duplicados o un error; con él, volver a correrlo es inofensivo.
    """
    escritas = 0
    for lote in lotes(filas, tam_lote):
        sb.table(tabla).upsert(lote, on_conflict=conflicto).execute()
        escritas += len(lote)
    return escritas
