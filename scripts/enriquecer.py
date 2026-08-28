"""
enriquecer.py — Traer el documento entero, no solo el titular.

El diagnóstico que lo motiva: los feeds RSS de los bancos centrales dan el
titular y poco más. Medido sobre la base, el Banco de Japón y el BCE
publican entradas con CERO caracteres de cuerpo, y el feed del FOMC repite
el titular como si fuera el texto. El LLM recibía 68 caracteres y, con la
regla de la cita activada, se negaba a inventar. Hacía bien.

Este script visita el enlace de cada noticia y guarda el documento real.

LÍMITE DELIBERADO: solo fuentes de NIVEL 1. Son organismos públicos que
publican sus comunicados precisamente para que se lean, sin muro de pago.
El Financial Times es nivel 3 y de pago: su titular seguirá siendo todo lo
que tengamos de él, y está bien que así sea.

    python scripts/enriquecer.py
    python scripts/enriquecer.py --limite 40 --min-cuerpo 800
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from comun import PERFILES_UA, conectar, limpiar_para_postgres, log

PAUSA = 1.5          # cortesía con servidores públicos
MAX_GUARDADO = 12_000

MAX_PAGINAS_PDF = 30   # las actas largas repiten; con 30 paginas sobra

try:
    import trafilatura
    HAY_TRAFILATURA = True
except ImportError:
    HAY_TRAFILATURA = False

try:
    import pdfplumber
    HAY_PDFPLUMBER = True
except ImportError:
    HAY_PDFPLUMBER = False


def extraer_pdf(contenido: bytes) -> str:
    """
    Texto de un PDF.

    No es un caso raro: el Banco de Japón publica en PDF el Money Stock y el
    Corporate Goods Price Index, y el BCE los discursos de sus consejeros.
    Son documentos primarios de primer nivel, justo la materia prima que
    este proyecto dice usar, y estaban quedándose fuera.

    Se leen hasta 30 páginas. Son PDF de texto, no escaneos, así que no
    hace falta OCR — si algún día aparece uno escaneado devolverá vacío y
    quedará registrado como "sin mejora" en vez de romper nada.
    """
    if not HAY_PDFPLUMBER:
        return ""
    try:
        partes = []
        with pdfplumber.open(io.BytesIO(contenido)) as pdf:
            for pagina in pdf.pages[:MAX_PAGINAS_PDF]:
                t = pagina.extract_text()
                if t:
                    partes.append(t)
        return re.sub(r"[ \t]+", " ", "\n".join(partes)).strip()
    except Exception as exc:
        print(f"      PDF ilegible: {type(exc).__name__}")
        return ""


def extraer_texto(html: str, url: str) -> str:
    """
    Saca el texto principal de la página.

    trafilatura hace esto bien: distingue el artículo de la navegación, los
    pies y los menús. Si no está instalada se cae a un limpiado a fuerza
    bruta, que funciona peor pero no deja el script inservible.
    """
    if HAY_TRAFILATURA:
        txt = trafilatura.extract(html, url=url, include_comments=False,
                                  include_tables=True, favor_precision=True)
        if txt and len(txt) > 200:
            return txt

    cuerpo = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>.*?</\1>",
                    " ", html, flags=re.S | re.I)
    cuerpo = re.sub(r"<[^>]+>", " ", cuerpo)
    for e, c in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&rsquo;", "'")):
        cuerpo = cuerpo.replace(e, c)
    return re.sub(r"\s+", " ", cuerpo).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Descarga el texto completo")
    ap.add_argument("--limite", type=int, default=30)
    ap.add_argument("--min-cuerpo", type=int, default=800,
                    help="por debajo de esto se considera que falta el documento")
    ap.add_argument("--nivel", type=int, default=1,
                    help="nivel máximo de fuente a visitar (1 = solo oficiales)")
    a = ap.parse_args()

    if not HAY_TRAFILATURA:
        print("\n  AVISO: trafilatura no está instalada. Se usará un limpiado")
        print("  de HTML a fuerza bruta, que arrastra menús y pies de página.")
        print(f"  Instálala con:  {sys.executable} -m pip install trafilatura\n")
    if not HAY_PDFPLUMBER:
        print("\n  AVISO: pdfplumber no está instalada. Los PDF se omitirán,")
        print("  y ahí es donde el Banco de Japón y el BCE publican sus datos.")
        print(f"  Instálala con:  {sys.executable} -m pip install pdfplumber\n")

    sb = conectar()

    fuentes = {f["id"]: f for f in
               sb.table("fuentes").select("id,nombre,nivel_confianza,ua_perfil")
               .lte("nivel_confianza", a.nivel).execute().data}
    if not fuentes:
        sys.exit("No hay fuentes de ese nivel.")

    candidatas = (sb.table("noticias")
                  .select("id,titular,cuerpo,url,fuente_id")
                  .in_("fuente_id", list(fuentes))
                  .not_.is_("url", "null")
                  .order("publicado_en", desc=True)
                  .limit(a.limite * 4).execute().data)

    pendientes = [n for n in candidatas
                  if len(n.get("cuerpo") or "") < a.min_cuerpo][:a.limite]

    if not pendientes:
        print("\n  Todas las noticias de nivel 1 ya tienen documento completo.")
        return

    print(f"\n{'='*72}")
    print(f"ENRIQUECIENDO {len(pendientes)} noticias de nivel <= {a.nivel}")
    print(f"{'='*72}")

    mejoradas = pdfs = fallos = 0
    for i, n in enumerate(pendientes, 1):
        f = fuentes[n["fuente_id"]]
        perfil = f.get("ua_perfil") or "contacto"
        antes = len(n.get("cuerpo") or "")
        print(f"\n  [{i}/{len(pendientes)}] {n['titular'][:58]}")
        print(f"      {f['nombre'][:34]:<36} cuerpo actual: {antes} car.")

        try:
            r = requests.get(n["url"], headers=PERFILES_UA[perfil], timeout=30)
            if r.status_code != 200:
                print(f"      HTTP {r.status_code}")
                fallos += 1
                continue

            tipo = r.headers.get("content-type", "")
            es_pdf = "pdf" in tipo.lower() or n["url"].lower().endswith(".pdf")

            if es_pdf:
                bruto = extraer_pdf(r.content)
                if not bruto:
                    print("      PDF sin texto extraíble (¿escaneado?)")
                    pdfs += 1
                    continue
            else:
                bruto = extraer_texto(r.text, n["url"])

            texto = limpiar_para_postgres(bruto)
            if len(texto) <= max(antes, 200):
                print(f"      sin mejora ({len(texto)} car.)")
                fallos += 1
                continue

            sb.table("noticias").update({
                "cuerpo": texto[:MAX_GUARDADO],
                # Vuelve a la cola: ahora hay documento que leer de verdad.
                "procesada_en": None,
            }).eq("id", n["id"]).execute()

            marca = " [PDF]" if es_pdf else ""
            print(f"      {antes} -> {len(texto):,} caracteres{marca}")
            mejoradas += 1

        except Exception as exc:
            print(f"      {type(exc).__name__}: {str(exc)[:70]}")
            fallos += 1

        time.sleep(PAUSA)

    log(sb, "enriquecer", None, True, filas=mejoradas)
    print(f"\n{'='*72}")
    print(f"  {mejoradas} noticias con documento completo")
    print(f"  {pdfs} PDFs omitidos   ·   {fallos} sin mejora")
    if mejoradas:
        print(f"\n  Las mejoradas volvieron a la cola de extracción.")
        print(f"  Siguiente:  .\\motor.ps1 extraer --limite 5 --seco")


if __name__ == "__main__":
    main()
