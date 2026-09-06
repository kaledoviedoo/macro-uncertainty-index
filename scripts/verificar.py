"""
verificar.py (comprobación contra el mundo real)

Antes de ingerir nada, comprueba las dos cosas que no se pueden asumir: que
cada ticker sembrado devuelve datos de verdad y que cada feed RSS oficial
sigue respondiendo. Marca los que funcionan en `activos.verificado` y
`fuentes.url_verificada`, y deja el resto en `ingesta_log`.

Con las fuentes prueba tres cabeceras distintas y guarda la que funcionó en
`fuentes.ua_perfil`: las agencias estadísticas de EE.UU. quieren un
identificador con contacto y los WAF comerciales solo hablan con
navegadores, así que no hay una que sirva para todas.

No escribe precios. Solo verifica.

    python scripts/verificar.py
    python scripts/verificar.py --solo-tickers
    python scripts/verificar.py --solo-fuentes
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

try:
    import feedparser
    import pandas as pd
    import requests
    import yfinance as yf
    from dotenv import load_dotenv
    from supabase import Client, create_client
except ImportError as e:
    sys.exit(f"Falta una dependencia: {e}\nEjecuta:  pip install -r requirements.txt")


RAIZ = Path(__file__).resolve().parent.parent

# La caché de zonas horarias de yfinance va a una ruta del perfil que en
# Windows puede acabar sincronizada; con varios hilos da "database is
# locked" y el ticker se marca como muerto estando vivo.
CACHE = Path.home() / ".cache" / "yfinance"
CACHE.mkdir(parents=True, exist_ok=True)
try:
    yf.set_tz_cache_location(str(CACHE))
except Exception:
    pass  # versiones antiguas de yfinance no exponen esta función

# Está medido: el BLS devuelve 403 ante un User-Agent de navegador y acepta
# uno con contacto; la OPEP y el FMI hacen lo contrario. Se prueban los tres
# perfiles en orden y se recuerda cuál funcionó para cada fuente.
CONTACTO = "kaledoviedoo@gmail.com"

PERFILES_UA = {
    # Primero el honesto: es lo que piden explícitamente las agencias
    # estadísticas de EE.UU., y es la forma correcta de identificarse.
    "contacto": {
        "User-Agent": f"MotorCausal/0.1 (+{CONTACTO})",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    },
    # Para los WAF comerciales que solo hablan con navegadores.
    "navegador": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        ),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "application/rss+xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    },
    # Algunos servidores viejos se atragantan con cabeceras largas.
    "minimo": {
        "User-Agent": "python-requests/2.32",
    },
}


# --------------------------------------------------------------------------
# Diagnóstico de la clave. "Invalid API key" no distingue entre una clave
# mal copiada, la equivocada y una rotada; esto lo resuelve antes de llamar.
# --------------------------------------------------------------------------
def describir_clave(clave: str) -> tuple[str, str | None]:
    """Devuelve (tipo legible, rol si se puede deducir)."""
    if clave.startswith("sb_secret_"):
        return "clave secreta moderna (sb_secret_...)", "service_role"
    if clave.startswith("sb_publishable_"):
        return "clave PUBLICABLE moderna (sb_publishable_...)", "anon"
    if clave.startswith("eyJ"):
        # JWT: la parte del medio es JSON en base64url. No verificamos la
        # firma, solo leemos qué rol declara.
        try:
            payload = clave.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            datos = json.loads(base64.urlsafe_b64decode(payload))
            return f"JWT heredado (rol declarado: {datos.get('role')})", datos.get("role")
        except Exception:
            return "JWT heredado, pero ilegible: probablemente está cortado", None
    return "formato no reconocido", None


def revisar_clave(clave_cruda: str) -> str:
    """Limpia la clave y aborta con un mensaje útil si algo huele mal."""
    problemas = []

    if clave_cruda != clave_cruda.strip():
        problemas.append("tiene espacios o saltos de línea alrededor")
    clave = clave_cruda.strip().strip('"').strip("'").strip()

    if any(c.isspace() for c in clave):
        problemas.append(
            "tiene un espacio o un salto de línea EN MEDIO. "
            "es lo que pasa cuando el portapapeles parte la clave en dos líneas"
        )
    if clave.startswith("pega_aqui") or not clave:
        sys.exit(
            "\nSUPABASE_SERVICE_KEY sigue con el valor de la plantilla.\n"
            "Ábrelo en: Dashboard > Project Settings > API Keys\n"
        )

    tipo, rol = describir_clave(clave)
    print(f"  Clave: {tipo}")
    print(f"  Longitud: {len(clave)} caracteres  ({clave[:6]}...{clave[-4:]})")

    if rol == "anon":
        sys.exit(
            "\n  ERROR: esa es la clave PÚBLICA, no la de servicio.\n\n"
            "  La pública solo puede leer (política RLS). Este script necesita\n"
            "  escribir los flags `verificado` y `url_verificada`.\n\n"
            "  Busca en el dashboard la que dice 'service_role' o 'secret'.\n"
            "  Viene oculta detrás de un botón 'Reveal' (hay que pulsarlo\n"
            "  antes de copiar, o copias la máscara en vez de la clave.\n"
        )

    if problemas:
        print()
        for p in problemas:
            print(f"  AVISO: la clave {p}")
        print("  Se limpió automáticamente, pero revisa el .env:")
        print("  la clave debe ir en UNA sola línea, sin comillas y sin espacios.")

    return clave


# --------------------------------------------------------------------------
# Conexión
# --------------------------------------------------------------------------
def conectar() -> Client:
    env = RAIZ / ".env"
    if not env.exists():
        sys.exit(f"No encuentro el archivo .env en {RAIZ}")
    load_dotenv(env, override=True)

    url = (os.getenv("SUPABASE_URL") or "").strip().strip('"').strip("'")
    if not url:
        sys.exit("Falta SUPABASE_URL en .env")

    clave = revisar_clave(os.getenv("SUPABASE_SERVICE_KEY") or "")
    sb = create_client(url, clave)

    # Sonda barata: falla aquí, con un mensaje claro, en vez de a mitad
    # del proceso con un stacktrace de pydantic.
    try:
        sb.table("activos").select("ticker").limit(1).execute()
    except Exception as exc:
        if "Invalid API key" in str(exc):
            sys.exit(
                "\n  Supabase rechaza la clave.\n\n"
                "  Las tres causas, por frecuencia:\n\n"
                "  1. Se copió la máscara en vez de la clave. En el dashboard\n"
                "     la service_role viene oculta: hay que pulsar el ojo o\n"
                "     'Reveal' ANTES de darle a copiar.\n\n"
                "  2. La clave quedó partida en dos líneas dentro del .env.\n"
                "     Tiene que ser una sola línea, por larga que sea.\n\n"
                "  3. Es de otro proyecto de Supabase. Comprueba que el\n"
                "     dashboard donde la copiaste dice 'motor-causal' arriba,\n"
                f"     y que la URL coincide con {url}\n"
            )
        raise

    return sb


def log(sb: Client, proceso: str, ticker: str | None, exito: bool,
        filas: int | None = None, error: str | None = None) -> None:
    """Todo fallo queda escrito. Un error silencioso es un error que vuelve."""
    sb.table("ingesta_log").insert({
        "proceso": proceso,
        "ticker": ticker,
        "exito": exito,
        "filas": filas,
        "error": (error or "")[:500] or None,
    }).execute()


# --------------------------------------------------------------------------
# 1. Tickers
# --------------------------------------------------------------------------
def verificar_tickers(sb: Client) -> None:
    # Solo los que vienen de Yahoo. COLCAP se sirve por SDMX del Banco de
    # la República y tiene su propio verificador en la fase 2; pedírselo a
    # Yahoo solo produce un fallo que no significa nada.
    activos = (
        sb.table("activos")
        .select("ticker,nombre")
        .eq("activo", True)
        .eq("fuente_datos", "yfinance")
        .execute()
        .data
    )
    simbolos = [a["ticker"] for a in activos]
    nombres = {a["ticker"]: a["nombre"] for a in activos}

    print(f"\n{'='*66}\nVERIFICANDO {len(simbolos)} TICKERS CONTRA YAHOO\n{'='*66}")

    # En lote, nunca en bucle: un bucle sobre 20 símbolos se gana un 429 (H-03).
    datos = yf.download(
        simbolos, period="1mo", interval="1d",
        group_by="ticker", auto_adjust=False, progress=False, threads=True,
    )

    def leer(tabla, t) -> tuple[int, float | None]:
        serie = tabla[t]["Close"].dropna() if len(simbolos) > 1 else tabla["Close"].dropna()
        return len(serie), (float(serie.iloc[-1]) if len(serie) else None)

    ok, sospechosos = [], []
    for t in simbolos:
        try:
            n, ultimo = leer(datos, t)
            # 10 sesiones en un mes es el mínimo para considerarlo vivo.
            if n >= 10:
                ok.append((t, n, ultimo))
            else:
                sospechosos.append(t)
        except Exception:
            sospechosos.append(t)

    # Segunda pasada, de uno en uno. Un fallo en lote puede ser un timeout
    # o una colisión de caché, y un falso negativo aquí se convierte en un
    # hueco silencioso en el histórico.
    fallidos = []
    if sospechosos:
        print(f"\n  Reintentando {len(sospechosos)} en serie: {', '.join(sospechosos)}\n")
        for t in sospechosos:
            resuelto = False
            # Ventana creciente: un instrumento poco líquido puede no
            # tener 10 sesiones en un mes y estar perfectamente vivo.
            for periodo, minimo in (("1mo", 10), ("6mo", 30), ("2y", 60)):
                try:
                    d = yf.download(t, period=periodo, interval="1d",
                                    auto_adjust=False, progress=False, threads=False)
                    serie = d["Close"].dropna()
                    if len(serie) >= minimo:
                        ultimo = float(serie.iloc[-1])
                        dias = (pd.Timestamp.today().normalize() - serie.index[-1]).days
                        ok.append((t, len(serie), ultimo))
                        if dias > 7:
                            print(f"  AVISO {t}: último dato hace {dias} días "
                                  f"(puede estar dejando de cotizar).")
                        resuelto = True
                        break
                except Exception as exc:
                    ultimo_error = f"{type(exc).__name__}: {exc}"[:120]
                time.sleep(1.0)

            if not resuelto:
                fallidos.append((t, "sin datos en ventanas de 1mo, 6mo ni 2y"))

    for t, n, ultimo in ok:
        print(f"  OK    {t:<12} {nombres[t][:30]:<32} {n:>3} sesiones   último={ultimo:,.2f}")
    for t, motivo in fallidos:
        print(f"  FALLO {t:<12} {nombres[t][:30]:<32} {motivo}")

    # Persistir el resultado: el flag `verificado` es lo que el ingestor
    # de la fase 2 consultará para saber a quién pedirle histórico.
    for t, n, _ in ok:
        sb.table("activos").update({"verificado": True}).eq("ticker", t).execute()
        log(sb, "verificar_ticker", t, True, filas=n)

    for t, motivo in fallidos:
        sb.table("activos").update({"verificado": False}).eq("ticker", t).execute()
        log(sb, "verificar_ticker", t, False, error=motivo)

    print(f"\n  Resumen: {len(ok)} verificados, {len(fallidos)} sin datos.")
    if fallidos:
        print("  Los fallidos quedan con verificado=false y NO se ingestan.")
        print("  Para índices latinoamericanos suele hacer falta un símbolo alternativo")
        print("  o una fuente distinta a Yahoo. Revísalos uno a uno antes de insistir.")


# --------------------------------------------------------------------------
# 2. Feeds oficiales
# --------------------------------------------------------------------------
def verificar_fuentes(sb: Client) -> None:
    fuentes = (
        sb.table("fuentes")
        .select("id,nombre,url_feed,nivel_confianza,tipo,ua_perfil")
        .not_.is_("url_feed", "null")
        .order("nivel_confianza")
        .execute()
        .data
    )

    print(f"\n{'='*66}\nVERIFICANDO {len(fuentes)} FEEDS\n{'='*66}")
    vivos = 0

    def intentar(url: str, perfil: str):
        """Devuelve (n_entradas, codigo_http, error) para un perfil dado."""
        try:
            r = requests.get(url, headers=PERFILES_UA[perfil], timeout=20)
            if r.status_code != 200:
                return 0, r.status_code, None
            return len(feedparser.parse(r.content).entries), 200, None
        except Exception as exc:
            return 0, None, f"{type(exc).__name__}"

    for f in fuentes:
        etiqueta = f"N{f['nivel_confianza']} {f['nombre'][:36]:<38}"

        # Si ya sabemos qué perfil funciona para esta fuente, se prueba primero.
        orden = list(PERFILES_UA)
        if f.get("ua_perfil") in PERFILES_UA:
            orden.remove(f["ua_perfil"])
            orden.insert(0, f["ua_perfil"])

        ganador, entradas, ultimo_fallo = None, 0, ""
        for perfil in orden:
            n, codigo, err = intentar(f["url_feed"], perfil)
            if n > 0:
                ganador, entradas = perfil, n
                break
            ultimo_fallo = err or f"HTTP {codigo}"
            time.sleep(0.3)

        if ganador:
            print(f"  OK    {etiqueta} {entradas:>3} entradas   [{ganador}]")
            sb.table("fuentes").update({
                "url_verificada": True, "activa": True, "ua_perfil": ganador,
            }).eq("id", f["id"]).execute()
            log(sb, "verificar_feed", None, True, filas=entradas)
            vivos += 1
        else:
            print(f"  FALLO {etiqueta} {ultimo_fallo} con los {len(orden)} perfiles")
            sb.table("fuentes").update({
                "url_verificada": False, "activa": False, "ua_perfil": None,
            }).eq("id", f["id"]).execute()
            log(sb, "verificar_feed", None, False,
                error=f"{f['nombre']}: {ultimo_fallo} en todos los perfiles")

        time.sleep(0.4)  # cortesía con servidores públicos

    print(f"\n  Resumen: {vivos}/{len(fuentes)} feeds vivos.")
    print("  Los caídos quedan activa=false. Las URLs de RSS cambian a menudo;")
    print("  búscalas de nuevo en la web oficial antes de descartar la fuente.")


# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Verificación de fase 1")
    p.add_argument("--solo-tickers", action="store_true")
    p.add_argument("--solo-fuentes", action="store_true")
    args = p.parse_args()

    sb = conectar()
    print(f"Conectado a {os.getenv('SUPABASE_URL')}")
    print(f"Hora: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    if not args.solo_fuentes:
        verificar_tickers(sb)
    if not args.solo_tickers:
        verificar_fuentes(sb)

    print(f"\n{'='*66}")
    print("Fase 1 cerrada. Consulta el estado con:")
    print("  select * from v_salud_ingesta;")
    print("  select nombre, nivel_confianza, url_verificada from fuentes order by 2;")


if __name__ == "__main__":
    main()
