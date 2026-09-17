"""Interfaz del modelo de lenguaje.

Los mensajes viajan en el formato de OpenAI porque es el que hablan DeepSeek,
OpenAI, Groq, Together, Mistral y Ollama. Cambiar de proveedor es cambiar
base_url y modelo en el .env; cambiar a uno con otro protocolo es escribir una
clase que cumpla este Protocol.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LlmResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_message: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LlmProvider(Protocol):
    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> LlmResponse: ...
    # Opcional: `chat(..., on_text=callback)` entrega el texto acumulado según
    # llega. El orquestador sólo lo usa si la firma del proveedor lo admite.
    # Opcional: abrir la conexión antes de que el usuario hable. La primera
    # llamada a la API paga DNS + TLS + handshake, y medido desde aquí eso es
    # ~1,4 s que se le suman justo a la primera frase, que es la que deja la
    # impresión. Se hace durante el arranque, mientras Whisper carga.
    async def warmup(self) -> None: ...
    async def close(self) -> None: ...
