"""Leer la pantalla como texto con posiciones, con el OCR que trae Windows.

Nova no tiene modelo de visión: DeepSeek no ve imágenes y en una GPU de 6 GB, ya
ocupada por Whisper y la voz clonada, no cabe uno. Lo que sí cabe —porque no
ocupa nada— es el OCR de Windows 11, el mismo que usa la herramienta de recortes.
Medido: 0,14 s para una pantalla de 1920x1080, 60-70 líneas con sus coordenadas,
español e inglés a la vez, sin descargar un solo byte.

Con eso la pantalla se convierte en una lista de líneas de texto con su posición.
No es «ver», pero para lo que un asistente necesita —«el segundo chat de la
lista», «el botón que dice Enviar», «qué pone en esa ventana»— es suficiente, y
un modelo de texto puede razonar sobre ella. Cada línea lleva un número; hacer
clic en la número 7 es hacer clic en el centro de su rectángulo.

Lo que sale de aquí va al modelo, o sea a DeepSeek. Es texto de tu pantalla:
por eso leerla es una acción de riesgo medio y no se hace sin avisar.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass

log = logging.getLogger("nova.ocr")

# Cuántas líneas se le enseñan al modelo como máximo. Una pantalla entera de
# código puede dar trescientas, y eso son tokens y ruido; las apps con las que
# se interactúa por voz rara vez pasan de ochenta.
MAX_LINES = 120


@dataclass(slots=True)
class ScreenLine:
    index: int
    text: str
    x: int          # esquina superior izquierda, en coordenadas de pantalla
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


@dataclass(slots=True)
class ScreenReading:
    lines: list[ScreenLine]
    origin: tuple[int, int]         # (left, top) de la zona capturada
    size: tuple[int, int]
    title: str
    taken_at: float

    def render(self, limit: int = MAX_LINES) -> str:
        """Lo que ve el modelo: una línea por texto, ordenadas de arriba abajo.

        Las coordenadas van relativas a la zona capturada, en píxeles, para que
        el modelo pueda distinguir «la columna de la izquierda» de «el centro»
        sin que le importe dónde está la ventana en el escritorio.
        """
        if not self.lines:
            return "No se leyó ningún texto."
        left, top = self.origin
        width, height = self.size
        out = [f"Ventana «{self.title}», {width}x{height}. Líneas leídas (n. x,y «texto»):"]
        for line in self.lines[:limit]:
            out.append(f"{line.index}. {line.x - left},{line.y - top} «{line.text}»")
        if len(self.lines) > limit:
            out.append(f"… y {len(self.lines) - limit} líneas más.")
        return "\n".join(out)


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        from winrt.windows.media.ocr import OcrEngine  # noqa: F401
        return True
    except ImportError:
        return False


async def read_png(png: bytes, origin: tuple[int, int], size: tuple[int, int],
                   title: str = "", language: str = "es") -> ScreenReading:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    engine = OcrEngine.try_create_from_language(Language(language)) \
        or OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows no tiene ningún idioma de OCR instalado")
    result = await engine.recognize_async(bitmap)

    left, top = origin
    lines: list[ScreenLine] = []
    for raw in result.lines:
        words = list(raw.words)
        if not words:
            continue
        x0 = min(w.bounding_rect.x for w in words)
        y0 = min(w.bounding_rect.y for w in words)
        x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words)
        y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words)
        text = raw.text.strip()
        if text:
            lines.append(ScreenLine(0, text, int(left + x0), int(top + y0),
                                    int(x1 - x0), int(y1 - y0)))

    # Orden de lectura: por filas (con tolerancia de media línea) y dentro de
    # cada fila de izquierda a derecha. Sin la tolerancia, dos textos de la misma
    # fila con un píxel de diferencia salen en orden absurdo.
    lines.sort(key=lambda l: (l.y // 12, l.x))
    for index, line in enumerate(lines, start=1):
        line.index = index
    return ScreenReading(lines, origin, size, title, time.monotonic())


def grab_png(box: dict[str, int] | None) -> tuple[bytes, tuple[int, int], tuple[int, int]]:
    """Captura con mss y devuelve (png, origen, tamaño)."""
    import mss
    import mss.tools

    with mss.mss() as sct:
        area = box or sct.monitors[1]
        raw = sct.grab(area)
        png = mss.tools.to_png(raw.rgb, raw.size)
        return png, (int(area["left"]), int(area["top"])), (raw.size.width, raw.size.height)


async def read_screen(box: dict[str, int] | None = None, title: str = "pantalla",
                      language: str = "es") -> ScreenReading:
    loop = asyncio.get_running_loop()
    png, origin, size = await loop.run_in_executor(None, grab_png, box)
    return await read_png(png, origin, size, title=title, language=language)
