"""LLM falso con reglas simples: permite probar todo el flujo sin gastar tokens."""
from __future__ import annotations

import re
import uuid
from typing import Any

from .base import LlmResponse, ToolCall


def _call(name: str, **arguments) -> ToolCall:
    return ToolCall(id=f"mock_{uuid.uuid4().hex[:8]}", name=name, arguments=arguments)


# El habla encadena órdenes con conjunciones; sin cortar por ellas, «abre el
# navegador y busca vuelos» acaba pidiendo abrir una app llamada
# «navegador y busca vuelos».
_CUT = re.compile(r"\s+(?:y|e|luego|despues|después|para|,)\s+")


def _first_target(text: str) -> str:
    return _CUT.split(text.strip(), maxsplit=1)[0].strip()


class MockLlm:
    def __init__(self) -> None:
        self._served: set[str] = set()

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> LlmResponse:
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = (message.get("content") or "").lower()
                break
        if messages and messages[-1].get("role") == "tool":
            return LlmResponse(text="Listo.")

        match = re.search(r"\b(abre|abrir|inicia)\s+(?:la\s+|el\s+)?(?:aplicación\s+)?([\w\s]+)", last_user)
        if match and "pestaña" not in last_user:
            target = _first_target(match.group(2))
            if target in ("chrome", "navegador", "el navegador"):
                return LlmResponse(tool_calls=[_call("browser_open")])
            return LlmResponse(tool_calls=[_call("open_application", application=target)])

        match = re.search(r"\b(cierra|cerrar)\s+(?:la\s+|el\s+)?([\w\s]+)", last_user)
        if match:
            target = _first_target(match.group(2))
            if "pestaña" in target:
                return LlmResponse(tool_calls=[_call("browser_close_tab")])
            return LlmResponse(tool_calls=[_call("close_application", application=target)])

        match = re.search(r"\b(busca|buscar)\s+(.+)", last_user)
        if match:
            return LlmResponse(tool_calls=[_call("browser_search", query=match.group(2).strip())])

        return LlmResponse(text="Estoy en modo simulado, así que sólo puedo responder frases cortas.")

    async def close(self) -> None:
        return None
