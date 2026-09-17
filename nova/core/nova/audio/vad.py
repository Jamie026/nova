"""Detección de actividad de voz sobre tramas de 10 ms.

webrtcvad es C puro, decide en microsegundos y no carga ningún modelo: es lo
correcto para algo que corre 100 veces por segundo durante horas. La histéresis
(start_ms / end_ms) es lo que evita cortar al usuario en mitad de una pausa.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import webrtcvad

from .format import FRAME_MS, SAMPLE_RATE


class VadEvent(Enum):
    NONE = "none"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"


@dataclass
class VadConfig:
    aggressiveness: int = 2
    start_ms: int = 180
    end_ms: int = 700


class VoiceActivityDetector:
    def __init__(self, config: VadConfig):
        self._vad = webrtcvad.Vad(max(0, min(3, config.aggressiveness)))
        self._start_frames = max(1, config.start_ms // FRAME_MS)
        self._end_frames = max(1, config.end_ms // FRAME_MS)
        self._voiced = 0
        self._silent = 0
        self.speaking = False

    def reset(self) -> None:
        self._voiced = 0
        self._silent = 0
        self.speaking = False

    def push(self, frame: bytes) -> VadEvent:
        is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
        if is_speech:
            self._voiced += 1
            self._silent = 0
        else:
            self._silent += 1
            self._voiced = 0

        if not self.speaking and self._voiced >= self._start_frames:
            self.speaking = True
            return VadEvent.SPEECH_START
        if self.speaking and self._silent >= self._end_frames:
            self.speaking = False
            return VadEvent.SPEECH_END
        return VadEvent.NONE
