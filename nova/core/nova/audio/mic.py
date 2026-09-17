"""Captura de micrófono: un único stream a 16 kHz mono int16 para todo el sistema.

Un solo stream abierto siempre (no abrir/cerrar por sesión): abrir un dispositivo
de audio cuesta 100-300 ms en Windows y eso se nota al despertar al agente.
Los consumidores (wake word, VAD, STT) reciben tramas de 10 ms desde una cola.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Optional

import numpy as np
import sounddevice as sd

from .format import FRAME_BYTES, FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE  # noqa: F401

log = logging.getLogger("nova.mic")


class MicrophoneStream:
    def __init__(self, device: Optional[int | str] = None, max_queue: int = 200):
        self._device = device
        self._raw: "queue.Queue[bytes]" = queue.Queue(maxsize=max_queue)
        self._stream: Optional[sd.RawInputStream] = None
        self._lock = threading.Lock()
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queue)
        self._pump_task: Optional[asyncio.Task] = None
        self._closing = False

    def _callback(self, indata, frames, time_info, status) -> None:  # hilo de PortAudio
        if status:
            log.debug("estado de audio: %s", status)
        try:
            self._raw.put_nowait(bytes(indata))
        except queue.Full:
            pass  # preferimos perder una trama a bloquear el hilo de audio

    async def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, device=self._device,
                dtype="int16", channels=1, callback=self._callback,
            )
            self._stream.start()
        self._closing = False
        self._pump_task = asyncio.create_task(self._pump(), name="mic-pump")
        log.info("micrófono abierto (%d Hz, tramas de %d ms)", SAMPLE_RATE, FRAME_MS)

    async def _pump(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._closing:
            try:
                chunk = await loop.run_in_executor(None, self._raw.get, True, 0.5)
            except queue.Empty:
                continue
            except Exception:
                break
            if self.frames.full():
                try:
                    self.frames.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            self.frames.put_nowait(chunk)

    async def stop(self) -> None:
        self._closing = True
        if self._pump_task:
            self._pump_task.cancel()
            self._pump_task = None
        with self._lock:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
        log.info("micrófono cerrado")

    def drain(self) -> None:
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except asyncio.QueueEmpty:
                break


def frame_rms(frame: bytes) -> float:
    """RMS lineal de una trama, 0..1. Para decidir, no para dibujar."""
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


def rms_level(frame: bytes) -> float:
    """Nivel 0..1 para animar el avatar. Escala log porque el oído es logarítmico."""
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(samples ** 2)))
    return float(min(1.0, max(0.0, (rms ** 0.5) * 2.2)))


def list_devices() -> list[dict]:
    out = []
    for index, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append({"id": index, "name": dev["name"], "kind": "input"})
        if dev.get("max_output_channels", 0) > 0:
            out.append({"id": index, "name": dev["name"], "kind": "output"})
    return out
