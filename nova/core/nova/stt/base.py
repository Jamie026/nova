"""Interfaz de transcripción. Cambiar de proveedor = escribir otra clase."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(slots=True)
class Transcript:
    text: str
    is_final: bool
    language: str = "es"


class SttProvider(Protocol):
    async def warmup(self) -> None: ...
    async def transcribe(self, pcm16: bytes, final: bool = True,
                         strict: bool = True, prompt: str | None = None) -> Transcript: ...
    # `prompt` sustituye al vocabulario habitual: al dictar letras, el de
    # aplicaciones sólo estorba.
    # `strict=False` salta el filtro de alucinaciones: en una confirmación,
    # «no, no, no, no» es una respuesta, no Whisper inventando.
    async def close(self) -> None: ...
