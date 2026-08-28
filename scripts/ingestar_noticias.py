"""
ingestar_noticias.py — Fase 4. La materia prima del razonamiento.

Lee los feeds RSS que sobrevivieron a la verificación y escribe en
`noticias`. Nada de embeddings ni de LLM aquí: solo texto, deduplicado y
fechado. Cuanto más tonto sea este paso, menos sitios donde equivocarse.

Dos cosas que hace y no se ven:

  · Usa el perfil de cabecera que cada fuente aceptó en la verificación.
    El BLS quiere un User-Agent con contacto; otros quieren uno de
    navegador. Ya lo sabemos, está guardado, no hay que redescubrirlo.

  · Marca `es_primaria` según el nivel de confianza de la fuente. Un
    comunicado del FOMC y una crónica del Financial Times sobre ese mismo
    comunicado no valen lo mismo, y el LLM debe saberlo.

    python scripts/ingestar_noticias.py
    python scripts/ingestar_noticias.py --dias 30
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import feedparser
import requests
from comun import PERFILES_UA, conectar, log, upsert

MAX_CUERPO = 12_000       # se recorta al escribir, no al leer


def limpiar(html: str) -> str:
    """Quita etiquetas sin traerse una dependencia entera para ello."""
    import re
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html or "",
                 flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = (txt.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
              .replace("&#39;", "'").replace("&rsquo;", "'"))
    return re.sub(r"\s+", " ", txt).strip()


def fecha_de(entrada) -> datetime | None:
    for campo in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entrada, campo, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def cuerpo_de(entrada) -> str:
    """El campo del cuerpo cambia de un feed a otro. Se coge el más largo."""
    trozos = []
    for c in getattr(entrada, "content", []) or []:
        trozos.append(c.get("value", ""))
    for campo in ("summary", "description", "subtitle"):
        v = getattr(entrada, campo, None)
        if v:
            trozos.append(v)
    return limpiar(max(trozos, key=len)) if trozos else ""


# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Ingesta de noticias por RSS")
    ap.add_argument("--dias", type=int, default=14,
                    help="antigüedad máxima a guardar")
    ap.add_argument("--nivel", type=int, default=3,
                    help="nivel de confianza máximo (1=solo primarias)")
    a = ap.parse_args()

    sb = conectar()
    fuentes = (sb.table("fuentes")
               .select("id,nombre,url_feed,nivel_confianza,tipo,ua_perfil")
               .eq("activa", True).eq("url_verificada", True)
               .lte("nivel_confianza", a.nivel)
               .order("nivel_confianza").execute().data)

    if not fuentes:
        sys.exit("No hay fuentes verificadas. Ejecuta verificar.py primero.")

    corte = datetime.now(timezone.utc) - timedelta(days=a.dias)
    print(f"\n{'='*72}\nNOTICIAS — {len(fuentes)} fuentes, últimos {a.dias} días\n{'='*72}")

    total_nuevas = 0
    for f in fuentes:
        perfil = f.get("ua_perfil") or "contacto"
        etiqueta = f"N{f['nivel_confianza']} {f['nombre'][:36]:<38}"
        try:
            r = requests.get(f["url_feed"], headers=PERFILES_UA[perfil], timeout=25)
            parsed = feedparser.parse(r.content)
        except Exception as exc:
            print(f"  {etiqueta} ERROR {type(exc).__name__}")
            log(sb, "ingestar_noticias", None, False, error=f"{f['nombre']}: {exc}"[:400])
            continue

        filas = []
        for e in parsed.entries:
            pub = fecha_de(e)
            if pub is None or pub < corte:
                continue
            url = getattr(e, "link", None)
            titular = limpiar(getattr(e, "title", ""))[:500]
            if not url or not titular:
                continue
            filas.append({
                "fuente_id": f["id"],
                "publicado_en": pub.isoformat(),
                "titular": titular,
                "cuerpo": cuerpo_de(e)[:MAX_CUERPO] or None,
                "url": url[:1000],
                # Nivel 1 es documento original: comunicado, acta, rueda de
                # prensa. Lo demás es alguien contando lo que dijo otro.
                "es_primaria": f["nivel_confianza"] == 1,
            })

        if not filas:
            print(f"  {etiqueta} sin entradas recientes")
            continue

        try:
            # on_conflict por url: reejecutar el script no duplica nada.
            n = upsert(sb, "noticias", filas, "url")
            print(f"  {etiqueta} {n:>3} entradas")
            log(sb, "ingestar_noticias", None, True, filas=n)
            total_nuevas += n
        except Exception as exc:
            print(f"  {etiqueta} ERROR al escribir: {str(exc)[:90]}")
            log(sb, "ingestar_noticias", None, False, error=str(exc)[:400])

        time.sleep(0.5)

    pend = (sb.table("noticias").select("id", count="exact")
            .is_("procesada_en", "null").execute())
    print(f"\n  {total_nuevas} entradas escritas.")
    print(f"  {pend.count} noticias esperando al lote de extracción.")
    print(f"\n  Siguiente:  python scripts/extraer.py --limite 20")


if __name__ == "__main__":
    main()
