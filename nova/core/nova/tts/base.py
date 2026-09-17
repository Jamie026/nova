"""Interfaz de síntesis de voz. Todo motor debe ser cancelable a mitad de frase."""
from __future__ import annotations

from typing import Optional, Protocol


class TtsProvider(Protocol):
    async def speak(self, text: str, on_level: Optional[callable] = None,
                    language: Optional[str] = None) -> bool:
        """Devuelve True si terminó de hablar, False si fue interrumpido.

        `language` es opcional y sólo lo entienden los motores multilingües: la
        sesión lo pasa cuando Nova tiene que decir algo en un idioma distinto del
        de su voz habitual, y reintenta sin él si el motor no lo acepta."""

    # Opcional: cargar el modelo antes de la primera respuesta. Con motores de
    # red (edge-tts) no hace falta; con uno local que sube pesos a la GPU son
    # 10-20 s que, si no se pagan en el arranque, los paga la primera frase.
    async def warmup(self) -> None: ...
    async def stop(self) -> None: ...
    async def close(self) -> None: ...
