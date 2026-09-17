"""Formato de audio compartido por todo el sistema.

Vive aparte de mic.py por una razón práctica: mic.py importa sounddevice, que a
su vez carga PortAudio al importarse. Teniendo las constantes aquí, el VAD y el
detector de palabra activadora —que son lógica pura— se pueden importar y probar
en una máquina sin tarjeta de sonido, que es justo donde corre la integración
continua. Es el mismo motivo por el que el troceado de frases del TTS no vive
dentro del motor de voz.

16 kHz mono int16 y tramas de 10 ms no son arbitrarios: es lo que piden a la vez
webrtcvad, Porcupine y Whisper, así que un único stream sirve a los tres sin
remuestrear.
"""
from __future__ import annotations

SAMPLE_RATE = 16_000
FRAME_MS = 10
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAME_BYTES = FRAME_SAMPLES * 2
