"""Pedir un dato letra por letra cuando de oído no sale.

Un correo o un apellido dichos de corrido son lo que peor entiende Whisper, y
adivinarlos manda el mensaje a otra persona. Así que Nova lo pide deletreado, lo
repite y sólo lo usa si el usuario dice que está bien.
"""
from __future__ import annotations

import uuid
from typing import Awaitable, Callable

from ..agent import spelling
from .registry import Risk, Tool, ToolResult

MAX_ATTEMPTS = 3
# Dictar, repetirlo y contestar, hasta tres veces.
TIMEOUT_S = 240.0

Dictate = Callable[[str], Awaitable[str]]
Confirm = Callable[[str, str, str], Awaitable[bool]]


def _valid(value: str, kind: str) -> bool:
    return spelling.is_email(value) if kind == "email" else len(value.replace(" ", "")) >= 2


def build(dictate: Dictate, confirm: Confirm) -> list[Tool]:
    async def spell_out(kind: str, label: str = "") -> ToolResult:
        what = label or ("el correo" if kind == "email" else "el nombre")
        prompt = f"¿Me dictas {what} letra por letra?"
        for _ in range(MAX_ATTEMPTS):
            heard = await dictate(prompt)
            if not heard:
                return ToolResult(False, "El usuario no dictó nada")
            if spelling.is_cancel(heard):
                return ToolResult(False, "El usuario canceló el dictado")
            value = spelling.decode(heard, kind)
            if not _valid(value, kind):
                prompt = "Uy, no te entendí bien. ¿Me lo dictas otra vez, letra por letra?"
                continue
            question = f"Te entendí {spelling.read_back(value)}. ¿Está bien así?"
            if await confirm(uuid.uuid4().hex[:8], question, "low"):
                return ToolResult(True, f"El usuario dictó y confirmó: {value}", {"value": value})
            prompt = "Dale, vamos de nuevo. Dímelo letra por letra."
        return ToolResult(False, f"No conseguí entender {what} tras {MAX_ATTEMPTS} intentos")

    return [
        Tool(
            name="spell_out",
            description=(
                "Pide al usuario que dicte un correo o un nombre letra por letra, se lo "
                "repite y devuelve lo que confirmó. Úsala si te falta la dirección, si no se "
                "entendió bien o si no se encontró el contacto. Es la única forma de "
                "conseguir el dato: no lo adivines ni lo completes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["email", "name"]},
                    "label": {"type": "string",
                              "description": "Qué se pide, p. ej. «el correo de Juan»"},
                },
                "required": ["kind"], "additionalProperties": False,
            },
            # Riesgo bajo: sólo pregunta. Lo que se haga con el dato lleva su propia
            # confirmación, y la repetición va dentro de la herramienta.
            handler=spell_out, risk=Risk.LOW, timeout_s=TIMEOUT_S,
        ),
    ]
