"""STT falso para desarrollar la interfaz sin gastar CPU ni APIs."""
from __future__ import annotations

import asyncio

from .base import Transcript

CANNED = [
    "abre el navegador y busca vuelos a cusco",
    "abre spotify",
    "cierra esa pestaña",
    "qué hora es",
]


class MockStt:
    def __init__(self) -> None:
        self._index = 0

    async def warmup(self) -> None:
        return None

    async def transcribe(self, pcm16: bytes, final: bool = True) -> Transcript:
        await asyncio.sleep(0.15)
        text = CANNED[self._index % len(CANNED)]
        if final:
            self._index += 1
            return Transcript(text, True)
        return Transcript(text[: max(4, len(text) // 2)], False)

    async def close(self) -> None:
        return None
