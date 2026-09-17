"""Contrato de mensajes entre el núcleo y la capa de escritorio.

Un único punto de verdad para el protocolo. Si cambia un nombre de evento, cambia
aquí y el editor marca el resto.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

CoreEventType = Literal[
    "state", "transcript", "assistant_text", "status", "tool_call", "tool_result",
    "confirm_request", "confirm_resolved", "level", "log", "settings", "error", "ready",
]

UIEventType = Literal[
    "text_input", "confirm_response", "stop", "wake", "get_settings",
    "set_settings", "shutdown", "ping",
]


class CoreEvent(TypedDict, total=False):
    type: CoreEventType
    state: str
    text: str
    spoken: str
    partial: bool
    name: str
    args: dict[str, Any]
    ok: bool
    summary: str
    id: str
    risk: str
    value: float
    level: str
    data: dict[str, Any]


def state_event(state: str) -> CoreEvent:
    return {"type": "state", "state": state}


def transcript(text: str, partial: bool) -> CoreEvent:
    return {"type": "transcript", "text": text, "partial": partial}


def status(text: str) -> CoreEvent:
    return {"type": "status", "text": text}


def assistant_text(text: str, spoken: str = "") -> CoreEvent:
    """`text` es el subtítulo; `spoken` es lo que sale por el altavoz.

    Son distintos cuando la voz habla un idioma y la pantalla muestra otro. Si
    coinciden se manda sólo `text`, para que la interfaz no tenga que decidir
    nada: lo que hay en `text` es siempre lo que se pinta.
    """
    event: CoreEvent = {"type": "assistant_text", "text": text}
    if spoken and spoken != text:
        event["spoken"] = spoken
    return event


def tool_call(name: str, args: dict[str, Any]) -> CoreEvent:
    return {"type": "tool_call", "name": name, "args": args}


def tool_result(name: str, ok: bool, summary: str) -> CoreEvent:
    return {"type": "tool_result", "name": name, "ok": ok, "summary": summary}


def confirm_request(request_id: str, text: str, risk: str) -> CoreEvent:
    return {"type": "confirm_request", "id": request_id, "text": text, "risk": risk}


def confirm_resolved(request_id: str, approved: bool) -> CoreEvent:
    """La pregunta ya tiene respuesta (por voz, por tiempo o por botón): la
    interfaz la quita. Sin esto, un «sí» dicho en voz alta dejaba el panel con
    los botones puestos, como si nadie hubiera contestado."""
    return {"type": "confirm_resolved", "id": request_id, "approved": approved}


def mic_level(value: float) -> CoreEvent:
    return {"type": "level", "value": value}


def error(text: str) -> CoreEvent:
    return {"type": "error", "text": text}
