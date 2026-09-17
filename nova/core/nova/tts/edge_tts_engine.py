"""TTS con edge-tts: voces neuronales en español, gratis y con streaming.

Tres cosas hacen que la respuesta suene seguida y empiece pronto:

  1. **Un solo stream de salida abierto** para toda la respuesta. Abrir un
     dispositivo de salida en Windows cuesta 150-400 ms y deja un chasquido;
     hacerlo una vez por frase era la mitad del problema de «lo dice a trozos».
  2. **Síntesis adelantada**: mientras suena un trozo ya se está pidiendo el
     siguiente. Cada `Communicate` abre un websocket nuevo contra Microsoft
     (300-600 ms), así que si esa espera no se solapa con la reproducción, se oye
     como un silencio en mitad de la frase.
  3. **El stream se abre a la vez que se pide el primer trozo**, no después: las
     dos esperas corren en paralelo y la primera sílaba sale antes.

La cancelación se revisa en cada bloque de 1024 muestras, así que el barge-in
corta el audio en menos de 50 ms.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf

from .text import plan_chunks

log = logging.getLogger("nova.tts")

# Las voces neuronales de edge-tts entregan MP3 mono a 24 kHz. Saberlo permite
# abrir el stream antes de tener el primer audio decodificado; si alguna voz
# devolviera otra cosa, _Player se reabre con el formato real.
_EXPECTED_RATE = 24_000
_EXPECTED_CHANNELS = 1


class _Player:
    """Un único stream de salida reutilizado por todos los trozos de una respuesta."""

    def __init__(self, device, cancel: asyncio.Event):
        self._device = device
        self._cancel = cancel
        self._stream: Optional[sd.OutputStream] = None
        self._rate = 0
        self._channels = 0

    def _ensure(self, rate: int, channels: int) -> None:
        if self._stream is not None and (self._rate, self._channels) == (rate, channels):
            return
        self._close_stream()
        self._stream = sd.OutputStream(samplerate=rate, channels=channels,
                                       dtype="float32", device=self._device)
        self._stream.start()
        self._rate, self._channels = rate, channels

    async def prepare(self) -> None:
        """Abre el dispositivo por adelantado con el formato esperado."""
        try:
            await asyncio.to_thread(self._ensure, _EXPECTED_RATE, _EXPECTED_CHANNELS)
        except Exception as exc:
            log.debug("no se pudo preabrir la salida de audio: %s", exc)

    async def play(self, mp3_bytes: bytes, on_level=None) -> bool:
        def _decode():
            return sf.read(io.BytesIO(mp3_bytes), dtype="float32", always_2d=True)

        data, rate = await asyncio.to_thread(_decode)
        loop = asyncio.get_running_loop()
        block = 1024

        def _write() -> bool:
            self._ensure(rate, data.shape[1])
            assert self._stream is not None
            for start in range(0, len(data), block):
                if self._cancel.is_set():
                    return False
                piece = data[start:start + block]
                self._stream.write(piece)
                if on_level is not None:
                    level = float(min(1.0, np.abs(piece).mean() * 6.0))
                    loop.call_soon_threadsafe(on_level, level)
            return True

        return await asyncio.to_thread(_write)

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        try:
            # abort() y no stop() cuando se ha cancelado: stop() espera a que se
            # vacíe el búfer, que es justo lo que no queremos si el usuario ha
            # interrumpido. En el final normal sí interesa drenarlo, porque ahí
            # está la última palabra de la frase.
            if self._cancel.is_set():
                stream.abort()
            else:
                stream.stop()
            stream.close()
        except Exception:
            pass

    async def close(self) -> None:
        await asyncio.to_thread(self._close_stream)


class EdgeTts:
    def __init__(self, voice: str = "es-PE-CamilaNeural", rate: str = "+8%",
                 volume: str = "+0%", device: Optional[int | str] = None):
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._device = device
        self._cancel = asyncio.Event()

    async def speak(self, text: str, on_level=None) -> bool:
        import edge_tts

        self._cancel.clear()
        chunks = plan_chunks(text)
        if not chunks:
            return True

        player = _Player(self._device, self._cancel)
        pending: Optional[asyncio.Task] = None
        completed = False
        try:
            # El primer trozo y la apertura del dispositivo, a la vez.
            pending = asyncio.create_task(self._synthesize(edge_tts, chunks[0]))
            await player.prepare()

            for index, _ in enumerate(chunks):
                audio = await pending
                pending = (
                    asyncio.create_task(self._synthesize(edge_tts, chunks[index + 1]))
                    if index + 1 < len(chunks) else None
                )
                if not audio or self._cancel.is_set():
                    return False
                if not await player.play(audio, on_level):
                    return False
            completed = True
            return True
        except Exception as exc:
            log.error("fallo de síntesis: %s", exc)
            return False
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
            if not completed:
                self._cancel.set()  # que _close_stream aborte en vez de drenar
            await player.close()

    async def _synthesize(self, edge_tts, sentence: str) -> bytes:
        communicate = edge_tts.Communicate(
            sentence, self._voice, rate=self._rate, volume=self._volume
        )
        buffer = bytearray()
        async for chunk in communicate.stream():
            if self._cancel.is_set():
                return b""
            if chunk["type"] == "audio":
                buffer.extend(chunk["data"])
        return bytes(buffer)

    async def stop(self) -> None:
        self._cancel.set()

    async def close(self) -> None:
        await self.stop()
