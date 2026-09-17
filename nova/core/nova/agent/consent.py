"""Interpretar un «sí» o un «no» dicho en voz alta.

Nova pregunta antes de lo que puede doler («¿Envío "hola" a Alain?»). Contestar
con el ratón rompe el flujo de una conversación de voz, así que aquí se decide
si lo que transcribió Whisper es una aceptación, un rechazo o ninguna de las dos.

Es deliberadamente conservador: lo que no se entiende con claridad NO es un sí.
Un «sí» mal reconocido envía un mensaje que no se deshace; un «no» mal
reconocido sólo obliga a repetir. Y por el mismo motivo, si en la misma frase
hay señales de las dos («sí, bueno, no»), gana el no.
"""
from __future__ import annotations

import re
import unicodedata

_YES = {
    "si", "sí", "dale", "ok", "okey", "okay", "vale", "hazlo", "claro", "adelante",
    "confirmo", "confirmado", "acepto", "aceptar", "envialo", "mandalo", "procede",
    "afirmativo", "correcto", "exacto", "eso", "va", "sip", "sep", "yes", "yeah",
    "yep", "sure", "go", "do it", "send it", "go ahead",
}
_NO = {
    "no", "nop", "nope", "cancela", "cancelar", "cancelalo", "para", "espera",
    "nada", "negativo", "mejor no", "no lo hagas", "no lo envies", "dejalo",
    "olvidalo", "cancel", "stop", "wait", "dont", "don't",
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^\w\s']", " ", text).split())


def interpret(text: str) -> bool | None:
    """True = sí, False = no, None = no se entiende (y por tanto no es un sí)."""
    cleaned = _normalize(text)
    if not cleaned:
        return None
    words = cleaned.split()
    # Más de seis palabras ya no es una respuesta a «¿sí o no?», es otra cosa:
    # probablemente una orden nueva o un ruido transcrito. No se arriesga.
    if len(words) > 6:
        return None

    said_no = any(p in cleaned.split() or f" {p} " in f" {cleaned} " for p in _NO)
    said_yes = any(p in cleaned.split() or f" {p} " in f" {cleaned} " for p in _YES)
    if said_no:
        return False
    if said_yes:
        return True
    return None
