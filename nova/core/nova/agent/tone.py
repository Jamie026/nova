"""Que Nova suene a una persona: limpiar lo que delata a un texto de máquina.

El prompt ya le pide hablar con naturalidad, pero los modelos tienen tics de
escritura que en voz alta suenan raros y en pantalla, a robot: guiones largos
para hacer incisos, viñetas, asteriscos de markdown. Aquí se quitan siempre,
aunque el modelo se olvide de la instrucción.

Lo que NO se toca: los guiones dentro de una palabra o un código («F001-000101»,
«Wi-Fi», «+51 987-654-321») y lo que va entre «comillas», que suele ser el
mensaje literal que el usuario va a enviar.
"""
from __future__ import annotations

import re

_QUOTED = re.compile(r"«[^»]*»")
_BULLET = re.compile(r"^\s*(?:[-*•·–—]|\d+[.)])\s+", re.M)
_MARKDOWN = re.compile(r"\*\*|__|`|^#+\s*", re.M)
# Un guion largo o medio, o uno corto con espacios a los lados, hace de inciso.
_DASH = re.compile(r"\s*[—–]\s*|\s+-\s+")


def _as_phrase(items: list[str]) -> str:
    """«a», «b», «c» → «a, b y c», sin el punto final de cada elemento."""
    items = [i.rstrip(" .;") for i in items]
    items = [i[:1].lower() + i[1:] if k and i[1:2].islower() else i for k, i in enumerate(items)]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1]


def _join_lists(text: str) -> str:
    """Las viñetas seguidas se convierten en una frase, como se diría en voz alta."""
    out: list[str] = []
    items: list[str] = []
    for line in text.split("\n"):
        if _BULLET.match(line):
            items.append(_BULLET.sub("", line).strip())
            continue
        if items:
            out.append(_as_phrase(items) + ".")
            items = []
        out.append(line)
    if items:
        out.append(_as_phrase(items) + ".")
    return "\n".join(out)


def naturalize(text: str) -> str:
    if not text:
        return text
    held: list[str] = []

    def _hold(match: re.Match) -> str:
        held.append(match.group(0))
        return f"\x00{len(held) - 1}\x00"

    out = _join_lists(_QUOTED.sub(_hold, text))
    out = _MARKDOWN.sub("", out)
    out = _DASH.sub(", ", out)
    out = re.sub(r"\s*\n\s*", " ", out)                 # una respuesta hablada no tiene saltos
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r",\s*([.!?])", r"\1", out)
    out = re.sub(r"^\s*,\s*", "", out)
    out = re.sub(r" {2,}", " ", out).strip()
    for index, literal in enumerate(held):
        out = out.replace(f"\x00{index}\x00", literal)
    return out
