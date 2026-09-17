"""Capturas de pantalla.

Se usa **mss** y no pyautogui/Pillow-ImageGrab por tres razones: es ctypes puro
sobre la API nativa (BitBlt en Windows), tarda ~15 ms en una pantalla 1080p, y
sabe recortar una región sin capturar antes el escritorio entero. Guarda PNG por
su cuenta, así que Pillow no entra en las dependencias del núcleo.

La captura de una *ventana* concreta no usa PrintWindow sino el rectángulo real
de la ventana en pantalla (lo pide el adaptador del SO) recortado sobre el
escritorio. Es menos elegante pero es lo que ve el usuario: si Discord está
tapado a medias por otra ventana, la captura lo refleja en lugar de mentir. Por
eso las herramientas enfocan la ventana antes de capturarla.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("nova.capture")

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(label: str, extension: str = ".png") -> str:
    """Nombre de archivo predecible y sin sorpresas a partir de texto hablado."""
    base = _SAFE.sub("-", (label or "captura").strip()).strip("-.") or "captura"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{base[:48]}-{stamp}{extension}"


@dataclass(slots=True)
class Shot:
    path: str
    width: int
    height: int
    region: str


class ScreenCapture:
    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)

    def _save(self, box: dict[str, int] | None, filename: str, region_label: str) -> Shot:
        import mss  # import perezoso: el núcleo arranca sin él en modo simulado
        import mss.tools

        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.output_dir / filename

        with mss.mss() as sct:
            # monitors[0] es el escritorio virtual completo (todos los monitores).
            area = box or sct.monitors[0]
            raw = sct.grab(area)
            mss.tools.to_png(raw.rgb, raw.size, output=str(target))

        log.info("captura guardada en %s (%dx%d)", target, raw.width, raw.height)
        return Shot(str(target), raw.width, raw.height, region_label)

    def full_screen(self, label: str = "pantalla") -> Shot:
        return self._save(None, safe_filename(label), "pantalla completa")

    def region(self, left: int, top: int, width: int, height: int, label: str = "region") -> Shot:
        if width <= 0 or height <= 0:
            raise ValueError("la región debe tener ancho y alto positivos")
        box = {"left": int(left), "top": int(top), "width": int(width), "height": int(height)}
        return self._save(box, safe_filename(label), f"región {width}x{height}")

    def window_rect(self, rect: tuple[int, int, int, int], label: str) -> Shot:
        left, top, right, bottom = rect
        return self.region(left, top, right - left, bottom - top, label)
