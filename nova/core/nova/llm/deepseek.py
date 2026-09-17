"""Proveedor DeepSeek (API compatible con OpenAI).

deepseek-chat soporta function calling, que es exactamente lo que necesita el
orquestador: el modelo elige una herramienta declarada en lugar de inventar
comandos de terminal. Si el modelo devuelve JSON malformado en los argumentos se
trata como error recuperable y se le devuelve al modelo como observación.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from openai import AsyncOpenAI

from .base import LlmResponse, ToolCall

log = logging.getLogger("nova.llm")


def _response(text: str, raw_calls: list[tuple[str, str, str]]) -> LlmResponse:
    calls: list[ToolCall] = []
    for call_id, name, arguments_json in raw_calls:
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            log.warning("argumentos no parseables en %s: %s", name, arguments_json)
            arguments = {"__parse_error__": arguments_json}
        calls.append(ToolCall(id=call_id, name=name, arguments=arguments))

    raw: dict[str, Any] = {"role": "assistant", "content": text}
    if raw_calls:
        raw["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments_json}}
            for call_id, name, arguments_json in raw_calls
        ]
    return LlmResponse(text=text, tool_calls=calls, raw_message=raw)


class DeepSeekProvider:
    def __init__(self, api_key: str, model: str = "deepseek-chat",
                 base_url: str = "https://api.deepseek.com",
                 temperature: float = 0.3, timeout: float = 60.0,
                 max_tokens: int | None = None):
        if not api_key:
            raise RuntimeError("Falta DEEPSEEK_API_KEY. Copia .env.example a .env y rellénala.")
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=2)
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None,
                   on_text: Callable[[str], None] | None = None) -> LlmResponse:
        """Con `on_text` la respuesta llega en streaming: se llama con el texto
        acumulado cada vez que llega un trozo. Sirve para empezar a hablar
        cuando la parte hablada está completa, sin esperar al subtítulo."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
        }
        # Generar tokens cuesta tiempo real: la respuesta se está leyendo en voz
        # alta y el usuario espera. El prompt ya pide una o dos frases; esto es
        # el tope duro por si el modelo se entusiasma.
        if self._max_tokens:
            kwargs["max_tokens"] = self._max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if on_text is None:
            completion = await self._client.chat.completions.create(**kwargs)
            message = completion.choices[0].message
            text = message.content or ""
            raw_calls = [(c.id, c.function.name, c.function.arguments or "")
                         for c in (message.tool_calls or [])]
        else:
            text, raw_calls = await self._stream(kwargs, on_text)
        return _response(text, raw_calls)

    async def _stream(self, kwargs: dict[str, Any],
                      on_text: Callable[[str], None]) -> tuple[str, list[tuple[str, str, str]]]:
        """Acumula los deltas. Las llamadas a herramientas llegan troceadas por
        índice (id y nombre en el primer delta, argumentos en los siguientes)."""
        pieces: list[str] = []
        calls: dict[int, list[str]] = {}
        stream = await self._client.chat.completions.create(**kwargs, stream=True)
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                pieces.append(delta.content)
                on_text("".join(pieces))
            for call in (delta.tool_calls or []):
                acc = calls.setdefault(call.index, ["", "", ""])
                if call.id:
                    acc[0] = call.id
                if call.function is not None:
                    if call.function.name:
                        acc[1] = call.function.name
                    if call.function.arguments:
                        acc[2] += call.function.arguments
        return "".join(pieces), [tuple(calls[i]) for i in sorted(calls)]

    async def warmup(self) -> None:
        """Abre la conexión con la API para que no la pague la primera frase.

        Un token de respuesta y sin herramientas: el coste es despreciable y
        ahorra ~1,4 s de handshake en la primera interacción, que es justo donde
        más se nota."""
        try:
            await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "hola"}],
                max_tokens=1,
                temperature=0.0,
            )
        except Exception as exc:
            # No es un fallo: si la API no responde ahora, ya se verá al hablar.
            log.debug("no se pudo precalentar la conexión con el modelo: %s", exc)

    async def close(self) -> None:
        await self._client.close()
