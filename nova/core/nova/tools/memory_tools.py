"""Herramientas para que el modelo guarde y quite lo que sabe del usuario."""
from __future__ import annotations

from ..agent.notes import UserNotes
from .registry import Risk, Tool, ToolResult


def build(notes: UserNotes) -> list[Tool]:
    async def remember(fact: str) -> ToolResult:
        added = notes.add(fact)
        return ToolResult(True, f"Guardado: «{fact}»" if added else f"Ya lo sabía: «{fact}»",
                          {"total": len(notes.all())})

    async def forget(fact: str) -> ToolResult:
        removed = notes.remove(fact)
        if not removed:
            return ToolResult(False, f"No tenía nada guardado que hable de «{fact}»")
        return ToolResult(True, f"Olvidado ({removed} nota{'s' if removed > 1 else ''})")

    async def list_memories() -> ToolResult:
        return ToolResult(True, notes.render(), {"notes": notes.all()})

    return [
        Tool(
            name="remember",
            description=(
                "Guarda un hecho o una preferencia del usuario para SIEMPRE, en todas las "
                "sesiones futuras. Úsala cuando el usuario diga «recuerda que…», «a partir de "
                "ahora…», «prefiero…», «siempre…», «mi … es …», o corrija algo que hiciste mal "
                "(«no, en Opera», «ábrelo en la pestaña actual»). Una frase corta y concreta, en "
                "tercera persona: «Usa Opera GX, no Chrome». No guardes la conversación ni cosas "
                "de un solo uso."
            ),
            parameters={"type": "object",
                        "properties": {"fact": {"type": "string", "maxLength": 200}},
                        "required": ["fact"], "additionalProperties": False},
            handler=remember, risk=Risk.LOW, timeout_s=5,
        ),
        Tool(
            name="forget",
            description="Borra de la memoria permanente las notas que contengan ese texto. "
                        "Úsala cuando el usuario diga «olvida…» o cambie de preferencia.",
            parameters={"type": "object",
                        "properties": {"fact": {"type": "string"}},
                        "required": ["fact"], "additionalProperties": False},
            handler=forget, risk=Risk.LOW, timeout_s=5,
        ),
        Tool(
            name="list_memories",
            description="Muestra todo lo que tienes guardado del usuario.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_memories, risk=Risk.LOW, timeout_s=5,
        ),
    ]
