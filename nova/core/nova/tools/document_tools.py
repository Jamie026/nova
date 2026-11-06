"""Leer tablas (Excel, CSV) y el texto de imágenes, todo en local y gratis.

Las imágenes se leen con RapidOCR (modelos PP-OCR de PaddleOCR sobre ONNX, en
CPU, sin internet): saca el texto (montos, nombres, fechas, números de
operación), no «ve» la foto. Para un comprobante o una factura es lo que hace
falta; para describir una foto, no sirve.

Por qué RapidOCR y no el OCR de Windows, medido con comprobantes sintéticos
(monto exacto; «MAL» = mal leído sin avisar):

                              192 normales          96 difíciles (borrosos,
                                                    sombra, foto de móvil...)
    OCR de Windows, 1 lectura   153 bien, 31 MAL      —
    Windows leído dos veces     144 bien, 5 MAL       39 bien, 1 MAL
    RapidOCR + su confianza     191 bien, 0 MAL       74 bien, 0 MAL

Una línea se marca dudosa (⚠) si RapidOCR la leyó con confianza < 0,95 o tiene
una letra metida entre cifras. Con 0,90 se marcaban menos, pero en los
difíciles pasaban 2 montos mal sin avisar.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from pathlib import Path

from ..capture import ocr
from ..files.search import FileSearcher
from ..files.tables import preview, read_table
from .registry import Risk, Tool, ToolResult

log = logging.getLogger("nova.tools.documents")

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")
DOUBTFUL = "⚠"
MIN_CONFIDENCE = 0.95
_MAX_SIDE = 4000            # RapidOCR reduce a 2000 px igualmente; esto sólo ahorra memoria

# Una letra pegada a una cifra dentro de un número: «5,2e7.28», «6,771.S3»,
# «1.276.@9», «h6:20». Es el error típico del OCR en montos.
_CURRENCY = re.compile(r"^(?:[Ss][/|,']|US\$|\$|€)")

# Un código que EMPIEZA por letras («F001-000101», «CL-0042», «B002») es un
# código, no un número con letras metidas: se le quita ese prefijo antes de mirar.
_CODE_PREFIX = re.compile(r"^[^\W\d_]{1,4}[-°º.]?(?=\d)")
# Una letra (o @) pegada a una cifra: «5,2e7.28», «6,771.S3», «1.276.@9».
_MIXED = re.compile(r"\d[.,]?[^\W\d_]|[^\W\d_][.,]?\d|\d[.,]?@|@[.,]?\d")


def _garbled(line: str) -> bool:
    """¿Hay una letra metida entre cifras? Un código que EMPIEZA por letras
    («F001-000101», «CL-0042», «B002») no cuenta: se le quita el prefijo antes."""
    for token in line.split():
        token = _CODE_PREFIX.sub("", _CURRENCY.sub("", token).strip(".,:;"))
        if any(c.isdigit() for c in token) and _MIXED.search(token):
            return True
    return False


def flag_lines(texts: list[str], scores: list[float]) -> tuple[str, int]:
    """El texto, con ⚠ delante de las líneas en las que no se puede confiar."""
    lines, doubtful = [], 0
    for text, score in zip(texts, scores):
        if score < MIN_CONFIDENCE or _garbled(text):
            lines.append(f"{DOUBTFUL} {text}")
            doubtful += 1
        else:
            lines.append(text)
    return "\n".join(lines), doubtful


_SECOND_LOOK_SCALE = 2.0
# Con 6 px de margen la línea se partía en trozos y bajaba la confianza; con 20 es estable.
_SECOND_LOOK_PAD = 20


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _second_look(image, box, text: str) -> float | None:
    """Vuelve a leer sólo esa línea, recortada y ampliada. Devuelve la confianza
    nueva si las cifras salen IGUALES; si cambian, nada (sigue dudosa)."""
    import numpy as np
    from PIL import Image

    xs, ys = [x for x, _ in box], [y for _, y in box]
    crop = image.crop((max(0, min(xs) - _SECOND_LOOK_PAD), max(0, min(ys) - _SECOND_LOOK_PAD),
                       min(image.size[0], max(xs) + _SECOND_LOOK_PAD),
                       min(image.size[1], max(ys) + _SECOND_LOOK_PAD)))
    crop = crop.resize((round(crop.size[0] * _SECOND_LOOK_SCALE), round(crop.size[1] * _SECOND_LOOK_SCALE)),
                       Image.LANCZOS)
    canvas = Image.new("RGB", (crop.size[0] + 40, crop.size[1] + 40), "white")
    canvas.paste(crop, (20, 20))
    _boxes, texts, scores = ocr.recognize(np.array(canvas))
    again = " ".join(texts)
    if not texts or _digits(again) != _digits(text):
        return None
    # La que cuenta es la de las cifras: el símbolo «S/» suele salir con menos.
    return min((v for t, v in zip(texts, scores) if _digits(t)), default=min(scores))


def ocr_image(data: bytes) -> tuple[list[str], list[float]]:
    """Las líneas de texto de una imagen y la confianza de cada una.

    Una línea con cifras leída con poca seguridad se lee otra vez, recortada y
    ampliada: si salen las mismas cifras con seguridad, deja de ser dudosa. Pasó
    con una factura limpia: «S/ 1,280.50» bien leído pero con 0,93 de confianza.
    """
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        if max(image.size) > _MAX_SIDE:
            image.thumbnail((_MAX_SIDE, _MAX_SIDE), Image.LANCZOS)
        boxes, texts, scores = ocr.recognize(np.array(image))
        for i, (box, text, score) in enumerate(zip(boxes, texts, scores)):
            if score < MIN_CONFIDENCE and len(_digits(text)) >= 2:
                better = _second_look(image, box, text)
                if better is not None and better >= MIN_CONFIDENCE:
                    scores[i] = better
    return texts, scores


DOCUMENT_EXTENSIONS = IMAGE_EXTENSIONS + (".pdf",)


def read_document(path: str | Path) -> tuple[list[str], list[float]]:
    """Líneas y confianza de una imagen o un PDF. El texto de un PDF generado
    por un sistema es exacto (confianza 1); uno escaneado pasa por el OCR."""
    from ..files.pdf import pdf_lines, pdf_page_images

    target = Path(path).expanduser()
    if target.suffix.lower() != ".pdf":
        return ocr_image(target.read_bytes())
    lines = pdf_lines(target)
    if lines:
        return lines, [1.0] * len(lines)
    texts: list[str] = []
    scores: list[float] = []
    for page in pdf_page_images(target):
        page_texts, page_scores = ocr_image(page)
        texts += page_texts
        scores += page_scores
    return texts, scores


def _resolved(searcher, path: str) -> str:
    """La ruta tal cual, o resuelta dentro de las carpetas permitidas si es relativa."""
    resolve = getattr(searcher, "resolve", None)
    return str(resolve(path)) if resolve is not None else path


def build(searcher: FileSearcher) -> list[Tool]:
    def _denied(path: str) -> ToolResult:
        return ToolResult(False, f"«{path}» está fuera de las carpetas permitidas")

    async def read_spreadsheet(path: str, sheet: str = "") -> ToolResult:
        path = _resolved(searcher, path)
        if not searcher.allows(path):
            return _denied(path)
        try:
            table = read_table(path, sheet)
        except (OSError, ValueError) as exc:
            return ToolResult(False, str(exc))
        if not table.columns:
            return ToolResult(False, f"«{Path(path).name}» no tiene datos")
        where = f" (hoja «{table.sheet}»)" if table.sheet else ""
        others = [s for s in table.sheets if s != table.sheet]
        extra = f"\nOtras hojas: {', '.join(others)}" if others else ""
        cut = f" (sólo leo las primeras {len(table.rows)})" if table.truncated else ""
        return ToolResult(True, f"{Path(path).name}{where}: {len(table.rows)} filas{cut}, "
                                f"columnas: {', '.join(table.columns)}{extra}\n{preview(table)}",
                          {"columns": table.columns, "rows": len(table.rows), "sheet": table.sheet})

    async def read_image(path: str) -> ToolResult:
        path = _resolved(searcher, path)
        if not searcher.allows(path):
            return _denied(path)
        target = Path(path).expanduser()
        if target.suffix.lower() not in DOCUMENT_EXTENSIONS:
            return ToolResult(False, f"«{target.name}» no es una imagen ni un PDF")
        try:
            texts, scores = await asyncio.to_thread(read_document, target)
        except ImportError:
            return ToolResult(False, "Falta RapidOCR: instálalo con npm run deps:core")
        except Exception as exc:
            log.exception("no pude leer %s", target)
            return ToolResult(False, f"No pude leer {target.name}: {exc}")
        if not texts:
            return ToolResult(False, f"No encontré texto en {target.name}: puede ser una foto sin "
                                     "texto o con letra demasiado pequeña o a mano.")
        text, doubtful = flag_lines(texts, scores)
        warning = (f" {doubtful} líneas marcadas con {DOUBTFUL} se leyeron con poca seguridad: NO "
                   "uses sus cifras sin que el usuario las confirme o las dicte."
                   if doubtful else " Todas las líneas se leyeron con seguridad.")
        return ToolResult(True, f"Texto de {target.name} (OCR).{warning}\n{text}",
                          {"path": str(target), "text": text, "doubtful": doubtful})

    return [
        Tool(
            name="read_spreadsheet",
            description=(
                "Lee un Excel (.xlsx), CSV o la tabla de un PDF: devuelve las hojas, las columnas, cuántas filas "
                "hay y las primeras filas. Úsala antes de un envío en lote para saber cómo se "
                "llaman las columnas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "sheet": {"type": "string", "default": "", "description": "Vacío = la primera"},
                },
                "required": ["path"], "additionalProperties": False,
            },
            handler=read_spreadsheet, risk=Risk.LOW, timeout_s=30,
        ),
        Tool(
            name="read_image",
            description=(
                "Lee el texto de una imagen o un PDF (captura, foto de un comprobante, factura) "
                "con OCR, en local y sin internet. Sirve para montos, nombres, fechas y números; "
                "no describe lo que se ve en una foto. Para la pantalla, usa read_screen."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            },
            handler=read_image, risk=Risk.LOW, timeout_s=30,
        ),
    ]
