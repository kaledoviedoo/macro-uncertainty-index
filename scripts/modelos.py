"""
modelos.py — Contrato de salida del LLM.

Estos modelos son la frontera entre el lenguaje y la base de datos. Todo lo
que el modelo diga y no quepa aquí, se descarta.

La decisión de diseño central: **no hay campo de dirección**. No se le
pregunta si el activo sube o baja, porque el centro de la distribución es el
parámetro que no sabemos estimar y pedirlo solo produce confianza falsa. Se
le pregunta cuánto se ensancha la incertidumbre y hacia qué lado engorda la
cola, que sí es contestable desde un documento.

La segunda decisión: `cita` es obligatoria y se verifica MECÁNICAMENTE
contra el texto original. Un LLM inventa una cita con la misma fluidez con
la que inventa un análisis; comprobar que la frase existe es un `in` de
Python, no una pregunta al modelo.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
# Normalización de la respuesta cruda.
#
# Los modelos responden en inglés aunque el prompt esté en español, y
# rebautizan campos con sinónimos razonables. Rechazar por eso sería exigir
# obediencia literal en algo que no cambia el contenido: `impacts` y
# `impactos` son la misma lista. Lo que NO se relaja es lo sustantivo —
# la cita, el rango del factor, la coherencia—, que se sigue validando
# igual de duro después.
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

    # Algunos modelos devuelven la lista de impactos sin envolverla.
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
    ¿Aparece esa frase en el documento?

    Coincidencia exacta tras normalizar, y si falla, cobertura de tokens en
    ventana: qué fracción de las palabras de la cita aparecen consecutivas
    en el texto. El umbral alto tolera un guion o un espacio raro pero no
    una reescritura.

    Esta función es la razón por la que la regla "sin cita no hay fila" es
    una garantía y no un ruego. El prompt puede pedirlo; esto lo comprueba.
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

    La distinción que importa: unas incoherencias son ERRORES DE FONDO y
    otras son descuidos de contabilidad. Un factor extremo con confianza
    baja es una contradicción real sobre el juicio. Pero decir "cola
    ninguna" y dejar la intensidad en 0.4 es un despiste sobre un campo
    que en ese caso no significa nada.

    Antes se descartaban ambos por igual y se perdían impactos válidos por
    un descuido de formato. Ahora los descuidos se CORRIGEN —la función
    modifica el objeto— y solo se rechaza lo que revela confusión real.
    """
    if imp.cola == Cola.NINGUNA and imp.intensidad_cola > 0.05:
        # Sin lado preferente, la intensidad no tiene referente. Se anula.
        imp.intensidad_cola = 0.0
    elif imp.cola != Cola.NINGUNA and imp.intensidad_cola < 0.05:
        # Nombró un lado: eso ya es afirmar asimetría. Se le pone la
        # intensidad más baja que sigue siendo una afirmación.
        imp.intensidad_cola = 0.20

    if imp.factor_incert > 2.0 and imp.confianza < 0.6:
        return False, "impacto extremo con confianza baja: contradicción de fondo"
    if abs(imp.factor_incert - 1.0) < 0.02:
        return False, "factor 1.0: no dice nada, mejor no escribirlo"
    return True, ""


ESQUEMA_JSON = Extraccion.model_json_schema()
