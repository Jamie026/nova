"""Leer la pantalla y las imágenes como texto con posiciones, con RapidOCR.

Nova no tiene modelo de visión: DeepSeek no ve imágenes y en una GPU de 6 GB, ya
ocupada por Whisper y la voz clonada, no cabe uno. Lo que sí cabe es un OCR:
RapidOCR (modelos PP-OCR de PaddleOCR sobre ONNX), en CPU, gratis, sin internet
y con ~30 MB de modelos que vienen con el paquete.

Antes se usaba el OCR de Windows. Medido con pantallas sintéticas de interfaz
(WhatsApp y Discord, claro, oscuro y morado, letra de 12 a 15 px; 441 textos):

                      texto bien    clic en su sitio    tiempo por pantalla
    OCR de Windows       92 %             91 %               0,06 s
    RapidOCR             97 %             97 %               1,3 s

y con comprobantes, el monto mal leído sin avisar bajó de 31 de 192 a 0 (ver
tools/document_tools.py). Es más lento, pero leer la pantalla mal y hacer clic
donde no es sale más caro que un segundo de espera.

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
import io
import logging
import threading
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
    score: float = 1.0      # confianza del OCR, de 0 a 1

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
        shown = self.lines[:limit]
        out = [f"Ventana «{self.title}», {width}x{height}. Líneas leídas (n. x,y «texto»), "
               f"agrupadas por columnas de izquierda a derecha; una lista (chats, pestañas, "
               f"resultados) son las líneas seguidas de una misma columna:"]
        for k, column in enumerate(self.columns(shown), start=1):
            xs = [l.x - left for l in column]
            out.append(f"— columna {k} (x≈{min(xs)}–{max(xs)}) —")
            for line in column:
                out.append(f"{line.index}. {line.x - left},{line.y - top} «{line.text}»")
        if len(self.lines) > limit:
            out.append(f"… y {len(self.lines) - limit} líneas más.")
        return "\n".join(out)

    @staticmethod
    def columns(lines: list["ScreenLine"], gap: int = 80) -> list[list["ScreenLine"]]:
        """Agrupa las líneas por su x: dos líneas están en la misma columna si
        empiezan a menos de `gap` píxeles una de otra (encadenado). Sin esto,
        el modelo leía la ventana de Discord como una sola lista y tomaba por
        «primer chat» el nombre del autor de los mensajes del panel derecho
        en vez de la primera entrada de la lista de la izquierda."""
        ordered = sorted(lines, key=lambda l: l.x)
        groups: list[list[ScreenLine]] = []
        for line in ordered:
            if groups and line.x - groups[-1][-1].x <= gap:
                groups[-1].append(line)
            else:
                groups.append([line])
        for group in groups:
            group.sort(key=lambda l: (l.y, l.x))
        return groups


_engine = None
_engine_lock = threading.Lock()     # una lectura a la vez: el motor no es reentrante


def available() -> bool:
    try:
        import rapidocr  # noqa: F401
        return True
    except ImportError:
        return False


def _rapidocr():
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR

        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        _engine = RapidOCR(params={"Global.log_level": "warning"})
    return _engine


def recognize(pixels) -> tuple[list, list[str], list[float]]:
    """(cajas de 4 puntos, textos, confianzas) de una imagen RGB como array."""
    with _engine_lock:
        result = _rapidocr()(pixels)
    boxes = [] if result.boxes is None else [[(float(x), float(y)) for x, y in box] for box in result.boxes]
    return boxes, list(result.txts or []), [float(v) for v in (result.scores or [])]


def _lines_from_png(png: bytes, origin: tuple[int, int]) -> list[ScreenLine]:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        pixels = np.array(image.convert("RGB"))
    left, top = origin
    lines: list[ScreenLine] = []
    for box, text, score in zip(*recognize(pixels)):
        text = text.strip()
        if not text:
            continue
        xs, ys = [x for x, _ in box], [y for _, y in box]
        x0, y0 = min(xs), min(ys)
        lines.append(ScreenLine(0, text, int(left + x0), int(top + y0),
                                int(max(xs) - x0), int(max(ys) - y0), score))
    return lines


async def read_png(png: bytes, origin: tuple[int, int], size: tuple[int, int],
                   title: str = "", language: str = "es") -> ScreenReading:
    """`language` se ignora: el modelo lee español e inglés a la vez."""
    lines = await asyncio.to_thread(_lines_from_png, png, origin)
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


async def read_window(os_adapter, handle: int, title: str = "ventana") -> ScreenReading:
    """Lee una ventana aunque otra la tape: la ventana se pinta a sí misma. Si
    no se puede (minimizada, o sale en negro), la parte de la pantalla donde está."""
    capture = getattr(os_adapter, "capture_window", None)
    shot = await asyncio.to_thread(capture, handle) if capture is not None else None
    if shot is not None:
        png, origin, size = shot
        return await read_png(png, origin, size, title=title)
    rect = os_adapter.window_rect(handle)
    if rect is None:
        raise RuntimeError("No pude medir la ventana")
    left, top, right, bottom = rect
    return await read_screen({"left": left, "top": top, "width": right - left, "height": bottom - top},
                             title=title)


async def read_screen(box: dict[str, int] | None = None, title: str = "pantalla",
                      language: str = "es") -> ScreenReading:
    loop = asyncio.get_running_loop()
    png, origin, size = await loop.run_in_executor(None, grab_png, box)
    return await read_png(png, origin, size, title=title, language=language)
