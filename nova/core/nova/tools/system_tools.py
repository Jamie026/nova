"""Herramientas de sistema: volumen, multimedia y portapapeles."""
from __future__ import annotations

from ..os_adapter.base import OsAdapter
from .registry import Risk, Tool, ToolResult


def build(os_adapter: OsAdapter) -> list[Tool]:
    async def set_volume(percent: int) -> ToolResult:
        ok = os_adapter.set_volume(percent)
        return ToolResult(ok, f"Volumen al {percent}%" if ok else "No pude cambiar el volumen")

    async def media_control(action: str) -> ToolResult:
        labels = {
            "play_pause": "Reproducción alternada", "next": "Siguiente pista",
            "previous": "Pista anterior", "stop": "Reproducción detenida", "mute": "Silencio alternado",
        }
        ok = os_adapter.media_key(action)
        return ToolResult(ok, labels.get(action, action) if ok else "Acción multimedia no soportada")

    async def read_clipboard() -> ToolResult:
        text = os_adapter.get_clipboard()
        if not text:
            return ToolResult(True, "El portapapeles está vacío", {"text": ""})
        return ToolResult(True, f"Portapapeles: {text[:500]}", {"text": text[:2000]})

    async def write_clipboard(text: str) -> ToolResult:
        ok = os_adapter.set_clipboard(text)
        return ToolResult(ok, "Copiado al portapapeles" if ok else "No pude escribir en el portapapeles")

    return [
        Tool(
            name="set_volume",
            description="Ajusta el volumen general del sistema entre 0 y 100.",
            parameters={
                "type": "object",
                "properties": {"percent": {"type": "integer", "minimum": 0, "maximum": 100}},
                "required": ["percent"], "additionalProperties": False,
            },
            handler=set_volume, risk=Risk.LOW, timeout_s=8,
        ),
        Tool(
            name="media_control",
            description="Controla la reproducción multimedia activa del sistema.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop", "mute"]}
                },
                "required": ["action"], "additionalProperties": False,
            },
            handler=media_control, risk=Risk.LOW, timeout_s=8,
        ),
        Tool(
            name="read_clipboard",
            description="Lee el contenido de texto del portapapeles.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=read_clipboard, risk=Risk.LOW, timeout_s=8,
        ),
        Tool(
            name="write_clipboard",
            description="Reemplaza el contenido del portapapeles con el texto indicado.",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"], "additionalProperties": False,
            },
            handler=write_clipboard, risk=Risk.MEDIUM, timeout_s=8,
            confirm_template="¿Reemplazo el portapapeles con «{text}»?",
        ),
    ]
