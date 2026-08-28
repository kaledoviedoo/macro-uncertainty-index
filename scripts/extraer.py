"""
extraer.py — Fase 5. El lote nocturno de sinapsis.

Lee las noticias pendientes, le pide al LLM la deformación de la
distribución de cada activo, y escribe en `impactos` solo lo que sobrevive
a tres filtros mecánicos.

Por qué en lote y de noche (H-01 de la auditoría): Groq gratuito da 8.000
tokens por minuto. Un solo prompt con una noticia y la lista de activos
consume buena parte de ese minuto. Poner esto en la ruta de la búsqueda del
usuario significaría 5-10 segundos de espera y la cuota agotada en una
tarde. El dashboard solo lee SQL.

Los tres filtros, en orden:

  1. PYDANTIC. Si el JSON no encaja en el esquema, la fila no existe.
  2. COHERENCIA. Reglas que un modelo rompe aunque el JSON valide:
     cola 'ninguna' con intensidad, factor 1.0 que no aporta nada.
  3. LA CITA. Se comprueba que la frase aparece LITERALMENTE en el
     documento. Este es el filtro que importa: un LLM inventa una cita
     con la misma fluidez con la que inventa un análisis, y comprobarlo
     es un `in` de Python, no una pregunta al modelo.

    python scripts/extraer.py --limite 20
    python scripts/extraer.py --limite 5 --seco     # sin escribir
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from dotenv import load_dotenv

from comun import RAIZ, conectar, log
from modelos import Extraccion, coherente, normalizar, verificar_cita
from prompts import PROMPT_VER, construir

# Límites de la capa gratuita, respetados por diseño y no por suerte.
#
# La cuenta: el sistema son ~1.060 tokens y el usuario llega a ~1.600 con un
# documento largo, así que cada llamada ronda los 2.700 entre entrada y
# salida. Groq da 8.000 tokens por MINUTO, no por petición: eso son unas 3
# llamadas por minuto. El cuello de botella no es el límite de 30 peticiones
# por minuto, es el de tokens, y por eso la pausa es de 20 segundos y no de 2.
#
# 20 noticias tardan unos 7 minutos. Es un lote nocturno: da igual.
PAUSA_ENTRE_LLAMADAS = 20.0
MAX_REINTENTOS = 3


# ---------------------------------------------------------------------------
# Orden de preferencia. Se usa el primero que la cuenta tenga disponible,
# porque los catálogos de la capa gratuita cambian sin aviso y un nombre
# de modelo escrito a mano caduca.
PREFERIDOS_GROQ = [
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "llama-3.1-8b-instant",
]

_RESUELTO: tuple[str, str, str] | None = None


def modelos_groq(clave: str) -> list[str]:
    """Qué modelos puede usar ESTA cuenta. No lo que dice la documentación."""
    try:
        r = requests.get("https://api.groq.com/openai/v1/models", timeout=30,
                         headers={"Authorization": f"Bearer {clave}"})
        if r.status_code != 200:
            print(f"  No pude listar modelos: HTTP {r.status_code} {r.text[:120]}")
            return []
        return sorted(m["id"] for m in r.json().get("data", []))
    except Exception as exc:
        print(f"  No pude listar modelos: {type(exc).__name__}")
        return []


def proveedor(verboso: bool = False) -> tuple[str, str, str]:
    """
    Elige proveedor y modelo. (nombre, url, modelo)

    Antes esto devolvía un nombre de modelo fijo y, si la cuenta no lo
    tenía, Groq respondía 404 — un error que parece de URL y es de
    catálogo. Ahora se pregunta a la API qué hay disponible y se elige de
    una lista de preferencia. Si nada encaja, se cae a Gemini.
    """
    global _RESUELTO
    if _RESUELTO and not verboso:
        return _RESUELTO

    # Cargar el .env AQUÍ y no confiar en que otro lo haya hecho. Antes solo
    # lo cargaba conectar(), así que `--modelos` —que no toca la base de
    # datos— se ejecutaba sin variables y juraba que no había claves.
    load_dotenv(RAIZ / ".env", override=True)

    def limpia(nombre: str) -> str:
        return (os.getenv(nombre) or "").strip().strip('"').strip("'")

    groq, gemini = limpia("GROQ_API_KEY"), limpia("GEMINI_API_KEY")

    if verboso:
        print(f"  .env leído de: {RAIZ / '.env'}")
        for n, v in (("GROQ_API_KEY", groq), ("GEMINI_API_KEY", gemini)):
            estado = f"{v[:7]}…{v[-4:]}  ({len(v)} caracteres)" if v else "ausente"
            print(f"    {n:<16} {estado}")
        print()

    if groq:
        disponibles = modelos_groq(groq)
        pedido = os.getenv("GROQ_MODEL")
        if verboso and disponibles:
            print(f"  Modelos disponibles en tu cuenta Groq ({len(disponibles)}):")
            for m in disponibles:
                print(f"    {m}")

        elegido = None
        if pedido and pedido in disponibles:
            elegido = pedido
        elif pedido and disponibles:
            print(f"  GROQ_MODEL='{pedido}' no está en tu cuenta. Se ignora.")
        if not elegido:
            elegido = next((m for m in PREFERIDOS_GROQ if m in disponibles), None)
        if not elegido and disponibles:
            # Cualquiera que sepa chatear, evitando audio y guardarraíles.
            elegido = next((m for m in disponibles
                            if not any(x in m for x in
                                       ("whisper", "tts", "guard", "embed"))), None)
        if elegido:
            _RESUELTO = ("groq", "https://api.groq.com/openai/v1/chat/completions",
                         elegido)
            return _RESUELTO
        print("  Groq no ofrece ningún modelo de chat a esta cuenta.")

    if gemini:
        print("  Usando Gemini.")
        _RESUELTO = ("gemini",
                     "https://generativelanguage.googleapis.com/v1beta/models",
                     os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
        return _RESUELTO

    sys.exit(
        f"\nNo encuentro ninguna clave utilizable en {RAIZ / '.env'}\n\n"
        "  El archivo debe tener una línea así, sin comillas y sin espacios\n"
        "  alrededor del signo igual:\n\n"
        "      GROQ_API_KEY=gsk_...\n\n"
        "  Comprueba también que la clave no quedó partida en dos líneas.\n"
        "  Para ver qué está leyendo:  .\\motor.ps1 extraer --modelos\n"
    )


def llamar(sistema: str, usuario: str) -> dict | None:
    prov, url, modelo = proveedor()

    for intento in range(MAX_REINTENTOS):
        try:
            if prov == "groq":
                r = requests.post(url, timeout=90, headers={
                    "Authorization": f"Bearer {os.getenv('GROQ_API_KEY')}",
                    "Content-Type": "application/json"},
                    json={"model": modelo,
                          "messages": [{"role": "system", "content": sistema},
                                       {"role": "user", "content": usuario}],
                          "temperature": 0.1,
                          "response_format": {"type": "json_object"}})
                if r.status_code == 429:
                    espera = float(r.headers.get("retry-after", 20))
                    print(f"      límite de tasa, esperando {espera:.0f}s")
                    time.sleep(espera + 1)
                    continue
                if r.status_code >= 400:
                    # El cuerpo dice QUÉ falló. Tragárselo y mostrar solo el
                    # código convierte un problema de catálogo en un misterio.
                    print(f"      HTTP {r.status_code}: {r.text[:220]}")
                    if r.status_code in (401, 403, 404):
                        return None      # reintentar no lo va a arreglar
                    r.raise_for_status()
                return json.loads(r.json()["choices"][0]["message"]["content"])

            r = requests.post(
                f"{url}/{modelo}:generateContent?key={os.getenv('GEMINI_API_KEY')}",
                timeout=90, json={
                    "systemInstruction": {"parts": [{"text": sistema}]},
                    "contents": [{"parts": [{"text": usuario}]}],
                    "generationConfig": {"temperature": 0.1,
                                         "responseMimeType": "application/json"}})
            if r.status_code == 429:
                time.sleep(30)
                continue
            if r.status_code >= 400:
                print(f"      HTTP {r.status_code}: {r.text[:220]}")
                if r.status_code in (401, 403, 404):
                    return None
                r.raise_for_status()
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt)

        except json.JSONDecodeError:
            print(f"      respuesta no es JSON (intento {intento+1})")
        except Exception as exc:
            print(f"      {type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(3 * (intento + 1))
    return None


# ---------------------------------------------------------------------------
def procesar(sb, noticia: dict, activos: list[dict], seco: bool) -> dict:
    """Devuelve un recuento de lo que pasó con esta noticia."""
    cuenta = {"propuestos": 0, "esquema": 0, "coherencia": 0, "cita": 0, "escritos": 0}

    sistema, usuario = construir(activos, noticia)
    bruto = llamar(sistema, usuario)
    if bruto is None:
        log(sb, "extraer", None, False, error=f"noticia {noticia['id']}: sin respuesta")
        return cuenta

    # Se traducen los sinónimos ANTES de validar. Un modelo que devuelve
    # `impacts` en vez de `impactos` no está equivocándose en el contenido,
    # solo en el idioma del campo. Lo sustantivo se sigue validando igual.
    try:
        ext = Extraccion.model_validate(normalizar(bruto))
    except Exception as exc:
        print(f"      esquema inválido: {str(exc)[:160]}")
        print(f"      claves recibidas: {list(bruto)[:8]}")
        cuenta["esquema"] = len(bruto.get("impactos") or bruto.get("impacts") or [])
        return cuenta

    cuenta["propuestos"] = len(ext.impactos)
    if not ext.es_relevante or not ext.impactos:
        print(f"      sin impactos  ·  {ext.resumen[:60]}")
        if not seco:
            sb.table("noticias").update(
                {"procesada_en": datetime.now(timezone.utc).isoformat()}
            ).eq("id", noticia["id"]).execute()
        return cuenta

    # El documento contra el que se verifican las citas.
    documento = f"{noticia.get('titular','')}\n{noticia.get('cuerpo') or ''}"
    validos = {a["ticker"] for a in activos}
    filas = []

    for imp in ext.impactos:
        if imp.ticker not in validos:
            print(f"      {imp.ticker}: ticker inventado, descartado")
            cuenta["esquema"] += 1
            continue

        ok, motivo = coherente(imp)
        if not ok:
            print(f"      {imp.ticker}: {motivo}")
            cuenta["coherencia"] += 1
            continue

        if not verificar_cita(imp.cita, documento):
            print(f"      {imp.ticker}: CITA NO ENCONTRADA — «{imp.cita[:55]}…»")
            cuenta["cita"] += 1
            continue

        filas.append({
            "noticia_id": noticia["id"], "ticker": imp.ticker,
            "canal": imp.canal.value, "horizonte_d": imp.horizonte_d,
            "factor_incert": round(imp.factor_incert, 3),
            "cola": imp.cola.value,
            "intensidad_cola": round(imp.intensidad_cola, 3),
            "cita": imp.cita, "cita_verificada": True,
            "salto": 0 if imp.confianza >= 0.8 else 1,
            "confianza": round(imp.confianza, 3),
            "modelo": proveedor()[2], "prompt_ver": PROMPT_VER,
        })
        flecha = {"izquierda": "↓", "derecha": "↑", "ninguna": "="}[imp.cola.value]
        print(f"      {imp.ticker:<11} x{imp.factor_incert:.2f} {flecha} "
              f"{imp.canal.value:<18} {imp.horizonte_d:>3}d  conf {imp.confianza:.2f}")

    # Tanda uniforme: tres o más impactos con factor, horizonte y cola
    # idénticos. No se descarta, se marca — la fase 6 dirá si estas filas
    # aciertan menos que las diferenciadas, que es la sospecha.
    if len(filas) >= 3:
        firmas = {(f["factor_incert"], f["horizonte_d"], f["cola"]) for f in filas}
        if len(firmas) == 1:
            for f in filas:
                f["lote_uniforme"] = True
            print(f"      AVISO: {len(filas)} impactos con valores idénticos."
                  f" Marcados como lote uniforme.")

    for f in filas:
        f["long_documento"] = len(documento)

    if filas and not seco:
        try:
            sb.table("impactos").upsert(
                filas, on_conflict="noticia_id,ticker,canal,prompt_ver").execute()
            cuenta["escritos"] = len(filas)
        except Exception as exc:
            print(f"      ERROR al escribir: {str(exc)[:90]}")
            log(sb, "extraer", None, False, error=str(exc)[:400])
    elif filas:
        cuenta["escritos"] = len(filas)

    if not seco:
        sb.table("noticias").update(
            {"procesada_en": datetime.now(timezone.utc).isoformat()}
        ).eq("id", noticia["id"]).execute()

    return cuenta


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Extracción estructurada")
    ap.add_argument("--limite", type=int, default=20)
    ap.add_argument("--seco", action="store_true", help="no escribe nada")
    ap.add_argument("--solo-primarias", action="store_true")
    ap.add_argument("--modelos", action="store_true",
                    help="lista los modelos que tu cuenta puede usar y sale")
    a = ap.parse_args()

    if a.modelos:
        print()
        prov, _, modelo = proveedor(verboso=True)
        print(f"\n  Elegido: {prov}/{modelo}")
        print("  Para fijar otro:  GROQ_MODEL=<id>  en el .env")
        return

    sb = conectar()
    prov, _, modelo = proveedor()

    activos = (sb.table("activos").select("ticker,nombre,tipo,region")
               .eq("activo", True).eq("verificado", True).execute().data)

    q = (sb.table("noticias")
         .select("id,titular,cuerpo,publicado_en,fuente_id,es_primaria")
         .is_("procesada_en", "null").order("publicado_en", desc=True)
         .limit(a.limite))
    if a.solo_primarias:
        q = q.eq("es_primaria", True)
    noticias = q.execute().data

    if not noticias:
        sys.exit("No hay noticias pendientes. Ejecuta ingestar_noticias.py.")

    fuentes = {f["id"]: f for f in
               sb.table("fuentes").select("id,nombre,nivel_confianza").execute().data}

    print(f"\n{'='*72}")
    print(f"EXTRACCIÓN  ·  {prov}/{modelo}  ·  prompt {PROMPT_VER}")
    print(f"{len(noticias)} noticias, {len(activos)} activos"
          f"{'   [SECO: no escribe]' if a.seco else ''}")
    print(f"{'='*72}")

    tot = {"propuestos": 0, "esquema": 0, "coherencia": 0, "cita": 0, "escritos": 0}
    for i, n in enumerate(noticias, 1):
        f = fuentes.get(n["fuente_id"], {})
        n["fuente_nombre"] = f.get("nombre", "?")
        n["nivel_confianza"] = f.get("nivel_confianza", "?")
        print(f"\n  [{i}/{len(noticias)}] {n['titular'][:66]}")
        print(f"      {n['fuente_nombre'][:40]}  ·  {str(n['publicado_en'])[:10]}")

        c = procesar(sb, n, activos, a.seco)
        for k in tot:
            tot[k] += c[k]
        time.sleep(PAUSA_ENTRE_LLAMADAS)

    print(f"\n{'='*72}\nRESUMEN\n{'='*72}")
    print(f"  Impactos propuestos por el modelo   {tot['propuestos']:>4}")
    print(f"  Rechazados por esquema o ticker     {tot['esquema']:>4}")
    print(f"  Rechazados por incoherencia         {tot['coherencia']:>4}")
    print(f"  RECHAZADOS POR CITA INVENTADA       {tot['cita']:>4}")
    print(f"  Escritos en la base                 {tot['escritos']:>4}")

    if tot["propuestos"]:
        tasa = tot["cita"] / tot["propuestos"] * 100
        print(f"\n  Tasa de citas inventadas: {tasa:.0f} %")
        print("  Vigílala entre versiones del prompt. Si sube, el modelo está")
        print("  rellenando huecos; si baja, el encargo está mejor planteado.")
    print("\n  Nada de esto es una predicción todavía. Son hipótesis con")
    print("  cita verificada, y valdrán algo cuando superen a `VIX > p80`.\n")


if __name__ == "__main__":
    main()
