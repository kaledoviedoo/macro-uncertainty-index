"""
modelos.py (contrato de salida del LLM y filtros de calidad)

Frontera entre el lenguaje y la base de datos: lo que el modelo diga y no
quepa aquí se descarta. Define el esquema Pydantic de un impacto, traduce
los sinónimos que devuelven los modelos, y aplica los filtros que separan
un análisis de un relleno con buena forma.

Dos decisiones sostienen el diseño:

  · No hay campo de dirección. No se pregunta si el activo sube o baja: el
    centro de la distribución es el parámetro que no sabemos estimar, y
    pedirlo solo produce confianza falsa. Se pregunta cuánto se ensancha la
    incertidumbre y hacia qué lado engorda la cola.
  · La cita es obligatoria y se verifica contra el texto original con un
    `in` de Python, no preguntándole al modelo. Un LLM inventa una cita con
    la misma fluidez con la que inventa un análisis.

Los filtros, en orden de aplicación:

  validar_por_partes   Valida impacto a impacto, no el documento entero, y
                       nombra el valor que causó cada rechazo.
  coherente            Corrige descuidos de contabilidad y rechaza solo las
                       contradicciones de fondo.
  verificar_cita       Coincidencia exacta tras normalizar, o 92 % de
                       cobertura de tokens en ventana.
  techo_de_titular     Recorta la confianza de documentos sin cuerpo.
  detectar_abanico     Marca el patrón de rellenar una tabla: varios
                       activos, un canal y factores en escalera.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


class Canal(str, Enum):
    """
    Por dónde llega el efecto. Obliga a nombrar un mecanismo concreto en
    lugar de decir "afecta negativamente", que no es una hipótesis
    falsable sino una impresión.
    """
    TASA = "tasa_interes"
    DIVISA = "divisa"
    DEMANDA = "demanda"
    OFERTA = "oferta"
    COSTES = "costes"
    REGULACION = "regulacion"
    FISCAL = "fiscal"
    GEOPOLITICO = "riesgo_geopolitico"
    LIQUIDEZ = "liquidez"


class Cola(str, Enum):
    IZQUIERDA = "izquierda"   # el riesgo se concentra a la baja
    DERECHA = "derecha"       # a la alza
    NINGUNA = "ninguna"       # más incertidumbre, sin lado preferente


class Impacto(BaseModel):
    """Una arista del grafo: un documento afectando a un activo."""

    ticker: str = Field(
        description="Ticker EXACTO de la lista suministrada. No inventar ni abreviar.")

    canal: Canal = Field(
        description="El mecanismo por el que el efecto llega a este activo.")

    horizonte_d: int = Field(
        ge=1, le=250,
        description="En cuántos días hábiles se materializa. 1-5 reacción "
                    "inmediata, 20 un mes, 60 un trimestre.")

    factor_incert: float = Field(
        ge=0.5, le=4.0,
        description="Multiplicador de la volatilidad habitual del activo en ese "
                    "horizonte. 1.0 = el documento no cambia la incertidumbre. "
                    "1.3 = 30 % más de dispersión esperada. Por encima de 2.0 "
                    "solo para rupturas mayores: guerra, default, quiebra "
                    "sistémica. La mayoría de noticias relevantes viven entre "
                    "1.05 y 1.4.")

    cola: Cola = Field(
        description="Hacia qué lado engorda la cola. 'ninguna' es una respuesta "
                    "legítima y frecuente: más incertidumbre sin lado claro.")

    intensidad_cola: float = Field(
        ge=0.0, le=1.0, default=0.0,
        description="Cuán marcada es la asimetría. 0 = simétrico. 1 = el riesgo "
                    "está casi todo en un lado. Debe ser 0 si cola='ninguna'.")

    cita: str = Field(
        min_length=20, max_length=600,
        description="Frase LITERAL Y CONTINUA copiada del documento, sin "
                    "reescribir, sin resumir, sin unir fragmentos separados. "
                    "Es lo que hace auditable la afirmación. Si no encuentras "
                    "una frase que la sostenga, NO incluyas este impacto.")

    confianza: float = Field(
        ge=0.0, le=1.0,
        description="Qué tan directo es el vínculo entre la cita y el activo. "
                    "0.9 el documento nombra al activo o su mercado. 0.5 hay "
                    "un eslabón intermedio evidente. 0.3 la conexión es "
                    "plausible pero indirecta.")

    razonamiento: str = Field(
        max_length=400,
        description="Una o dos frases: de la cita al activo, paso a paso. Se "
                    "muestra al usuario en la Terminal de Sinapsis.")

    @field_validator("ticker")
    @classmethod
    def limpiar_ticker(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("cita")
    @classmethod
    def cita_no_generica(cls, v: str) -> str:
        v = " ".join(v.split())
        # Frases que un modelo produce cuando no encontró nada y rellena.
        vacias = ("el documento", "la noticia", "el texto", "según el",
                  "no se menciona", "n/a", "no aplica")
        if v.lower().strip().startswith(vacias):
            raise ValueError("la cita describe el documento en vez de citarlo")
        return v


class Extraccion(BaseModel):
    """Lo que devuelve el modelo por cada documento procesado."""

    resumen: str = Field(
        default="", max_length=600,
        description="Qué dice el documento, en una frase, sin interpretar "
                    "consecuencias de mercado.")

    es_relevante: bool = Field(
        default=True,
        description="False si el documento no tiene ninguna implicación para "
                    "los activos de la lista. La mayoría de comunicados "
                    "oficiales NO son relevantes, y decirlo es la respuesta "
                    "correcta, no un fracaso.")

    impactos: list[Impacto] = Field(
        default_factory=list, max_length=12,
        description="Vacío si es_relevante es false. Solo activos con una cita "
                    "que sostenga el vínculo.")


# ---------------------------------------------------------------------------
# Sinónimos. Los modelos responden en inglés aunque el prompt esté en
# español; `impacts` e `impactos` son la misma lista. Lo sustantivo (cita,
# rango del factor, coherencia) se sigue validando igual de duro después.
# ---------------------------------------------------------------------------
_ALIAS_RAIZ = {
    "impacts": "impactos", "impact": "impactos", "items": "impactos",
    "results": "impactos", "resultados": "impactos",
    "summary": "resumen", "sumario": "resumen",
    "is_relevant": "es_relevante", "relevant": "es_relevante",
    "relevante": "es_relevante",
}

_ALIAS_IMPACTO = {
    "symbol": "ticker", "asset": "ticker", "activo": "ticker",
    "channel": "canal", "mechanism": "canal",
    "horizon": "horizonte_d", "horizon_d": "horizonte_d",
    "horizon_days": "horizonte_d", "horizonte": "horizonte_d",
    "uncertainty_factor": "factor_incert", "factor": "factor_incert",
    "uncertainty": "factor_incert", "factor_incertidumbre": "factor_incert",
    "tail": "cola", "tail_side": "cola",
    "tail_intensity": "intensidad_cola", "intensidad": "intensidad_cola",
    "quote": "cita", "citation": "cita", "evidence": "cita",
    "confidence": "confianza",
    "reasoning": "razonamiento", "rationale": "razonamiento",
    "explanation": "razonamiento",
}

_ALIAS_VALOR = {
    "left": "izquierda", "downside": "izquierda", "down": "izquierda",
    "right": "derecha", "upside": "derecha", "up": "derecha",
    "none": "ninguna", "neutral": "ninguna", "both": "ninguna",
    "interest_rate": "tasa_interes", "rates": "tasa_interes",
    "rate": "tasa_interes", "currency": "divisa", "fx": "divisa",
    "demand": "demanda", "supply": "oferta", "costs": "costes",
    "regulation": "regulacion", "fiscal_policy": "fiscal",
    "geopolitical": "riesgo_geopolitico", "geopolitical_risk": "riesgo_geopolitico",
    "liquidity": "liquidez",
}


def normalizar(bruto: dict) -> dict:
    """Traduce sinónimos al vocabulario del esquema. No inventa nada."""
    if not isinstance(bruto, dict):
        return {}

    out = {_ALIAS_RAIZ.get(k, k): v for k, v in bruto.items()}

    if "impactos" not in out and all(k in out for k in ("ticker", "cita")):
        out = {"impactos": [bruto]}

    imps = []
    for i in out.get("impactos") or []:
        if not isinstance(i, dict):
            continue
        n = {_ALIAS_IMPACTO.get(k, k): v for k, v in i.items()}
        for campo in ("cola", "canal"):
            v = n.get(campo)
            if isinstance(v, str):
                n[campo] = _ALIAS_VALOR.get(v.strip().lower(), v.strip().lower())
        imps.append(n)
    out["impactos"] = imps

    if "es_relevante" not in out:
        out["es_relevante"] = bool(imps)
    return out


# ---------------------------------------------------------------------------
# Validación impacto a impacto. Pydantic es todo o nada sobre el objeto
# completo, así que un `canal` mal etiquetado en el tercer impacto tumbaba
# los dos primeros. Un documento no es una unidad de verdad: es una lista de
# afirmaciones independientes.
# ---------------------------------------------------------------------------
def _motivo(exc: ValidationError, crudo: dict) -> str:
    """
    Describe el fallo con el valor que lo causó. Sin él no se distingue una
    barbaridad de un sinónimo que solo falta en `_ALIAS_VALOR`.
    """
    partes = []
    for e in exc.errors()[:3]:
        campo = ".".join(str(x) for x in e["loc"]) if e["loc"] else "?"
        valor = crudo.get(e["loc"][0]) if e["loc"] else None
        if isinstance(valor, str) and len(valor) > 45:
            valor = valor[:45] + "…"
        partes.append(f"{campo}=«{valor}» ({e['msg'][:55]})")
    return "; ".join(partes)


def validar_por_partes(bruto: dict) -> tuple[Extraccion, list[str]]:
    """
    Valida el envoltorio y cada impacto por separado. Devuelve los que
    sobrevivieron y los motivos de los que no. Nunca lanza.
    """
    datos = normalizar(bruto)
    crudos = [c for c in (datos.pop("impactos", None) or []) if isinstance(c, dict)]

    try:
        ext = Extraccion.model_validate({**datos, "impactos": []})
    except ValidationError:
        # Ni el envoltorio vale: se conserva lo mínimo para seguir
        # examinando los impactos, que es donde está el contenido.
        ext = Extraccion(resumen=str(datos.get("resumen") or "")[:600],
                         es_relevante=bool(crudos), impactos=[])

    rechazos: list[str] = []
    for i, c in enumerate(crudos[:12]):
        try:
            ext.impactos.append(Impacto.model_validate(c))
        except ValidationError as exc:
            etiqueta = str(c.get("ticker") or f"impacto {i}")
            rechazos.append(f"{etiqueta}: {_motivo(exc, c)}")

    if ext.impactos:
        ext.es_relevante = True
    return ext, rechazos


# ---------------------------------------------------------------------------
# Detectores de relleno
# ---------------------------------------------------------------------------
# Un documento puede tocar de verdad a varios activos (una decisión de tipos
# mueve medio mercado) y entonces los factores salen distintos, porque cada
# activo tiene su exposición. Lo que delata el relleno es la escalera: mismo
# canal, misma cola y factores casi iguales. Por eso se mide la dispersión y
# no la igualdad, que es lo que hacía el detector anterior sin saltar nunca.
ABANICO_MIN = 3
ABANICO_DISPERSION = 0.10

# Un titular no es un documento. El FT llega sin cuerpo, así que la cita se
# verifica contra unos 150 caracteres y "sin cita no hay fila" se cumple
# solo: deja de ser una garantía. De ahí se sostiene una afirmación, no
# cinco, y ni esa con confianza alta. El techo no tira la noticia (un
# titular sobre el crudo dice algo real) sino que reduce su peso, porque el
# aporte a la varianza es conf · (factor² − 1).
MIN_CUERPO_PARA_VARIOS = 600
CONF_MAX_TITULAR = 0.35


def techo_de_titular(confianza: float, largo_doc: int) -> tuple[float, bool]:
    """Confianza aplicable y si hubo recorte. Un titular no afirma con fuerza."""
    if largo_doc >= MIN_CUERPO_PARA_VARIOS or confianza <= CONF_MAX_TITULAR:
        return confianza, False
    return CONF_MAX_TITULAR, True


def detectar_abanico(filas: list[dict]) -> tuple[bool, str]:
    """¿Mismo canal, misma cola y factores casi iguales? Entonces es relleno."""
    if len(filas) < ABANICO_MIN:
        return False, ""
    if len({f["canal"] for f in filas}) > 1:
        return False, ""
    if len({f["cola"] for f in filas}) > 1:
        return False, ""

    fs = [float(f["factor_incert"]) for f in filas]
    disp = max(fs) - min(fs)
    if disp >= ABANICO_DISPERSION:
        return False, ""
    return True, (f"{len(filas)} impactos · un canal ({filas[0]['canal']}) · "
                  f"una cola ({filas[0]['cola']}) · factores en {disp:.2f}")


# ---------------------------------------------------------------------------
# Verificación mecánica de la cita.
# ---------------------------------------------------------------------------
def _normalizar(t: str) -> str:
    """Minúsculas, sin acentos, espacios colapsados, sin puntuación suelta."""
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def verificar_cita(cita: str, documento: str, umbral: float = 0.92) -> bool:
    """
    ¿Aparece esa frase en el documento? Coincidencia exacta tras normalizar
    y, si falla, cobertura de tokens en ventana. El umbral alto tolera un
    guion o un espacio raro pero no una reescritura.

    Es lo que convierte "sin cita no hay fila" en una garantía y no en un
    ruego: el prompt puede pedirlo, esto lo comprueba.
    """
    c, d = _normalizar(cita), _normalizar(documento)
    if not c or not d:
        return False
    if c in d:
        return True

    tc, td = c.split(), d.split()
    if len(tc) < 3 or len(td) < len(tc):
        return False

    objetivo = set(tc)
    mejor = 0.0
    for i in range(len(td) - len(tc) + 1):
        ventana = set(td[i:i + len(tc)])
        mejor = max(mejor, len(objetivo & ventana) / len(objetivo))
        if mejor >= umbral:
            return True
    return mejor >= umbral


def coherente(imp: Impacto) -> tuple[bool, str]:
    """
    Coherencias internas que un modelo rompe aunque el JSON valide.

    Distingue dos cosas. Un factor extremo con confianza baja es una
    contradicción de fondo y se rechaza. Decir "cola ninguna" y dejar la
    intensidad en 0,4 es un descuido sobre un campo que ahí no significa
    nada: se corrige (la función modifica el objeto) en vez de perder un
    impacto válido por un detalle de formato.
    """
    if imp.cola == Cola.NINGUNA and imp.intensidad_cola > 0.05:
        imp.intensidad_cola = 0.0
    elif imp.cola != Cola.NINGUNA and imp.intensidad_cola < 0.05:
        imp.intensidad_cola = 0.20

    if imp.factor_incert > 2.0 and imp.confianza < 0.6:
        return False, "impacto extremo con confianza baja: contradicción de fondo"
    if abs(imp.factor_incert - 1.0) < 0.02:
        return False, "factor 1.0: no dice nada, mejor no escribirlo"
    return True, ""


ESQUEMA_JSON = Extraccion.model_json_schema()
