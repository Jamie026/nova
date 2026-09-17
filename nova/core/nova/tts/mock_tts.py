"""TTS falso: respeta tiempos y cancelación para poder probar el barge-in."""
from __future__ import annotations

import asyncio


class MockTts:
    def __init__(self) -> None:
        self._cancel = asyncio.Event()

    async def speak(self, text: str, on_level=None) -> bool:
        self._cancel.clear()
        duration = min(6.0, max(0.6, len(text) / 16))
        steps = int(duration / 0.05)
        for step in range(steps):
            if self._cancel.is_set():
                return False
            if on_level is not None:
                on_level(0.35 + 0.3 * ((step % 7) / 7))
            await asyncio.sleep(0.05)
        return True

    async def stop(self) -> None:
        self._cancel.set()

    async def close(self) -> None:
        await self.stop()
