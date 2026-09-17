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
    async def transcribe(self, pcm16: bytes, final: bool = True) -> Transcript: ...
    async def close(self) -> None: ...
