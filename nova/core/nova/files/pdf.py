"""PDF: el texto de una factura y las tablas de una lista de clientes.

Dos clases de PDF y se distinguen solas:

* **Con texto** (los que genera un sistema: facturas electrónicas, reportes).
  El texto se saca tal cual, sin OCR: es exacto, así que ninguna línea es dudosa.
* **Escaneados** (una foto o un escáner metido en PDF). No tienen texto: cada
  página se convierte en imagen y se lee con RapidOCR, como una foto.
"""
from __future__ import annotations

from pathlib import Path

MAX_PAGES = 10              # una factura no tiene más; evita leer un libro por error
_RENDER_SCALE = 2.5         # ~180 ppp: el OCR lee bien la letra pequeña de una factura
_MIN_TEXT_CHARS = 25        # menos que esto en una página = no tiene capa de texto


def pdf_lines(path: str | Path) -> list[str]:
    """Las líneas de texto del PDF; vacía si es escaneado."""
    import pdfplumber

    lines: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages[:MAX_PAGES]:
            text = page.extract_text() or ""
            if len(text.strip()) >= _MIN_TEXT_CHARS:
                lines.extend(line.strip() for line in text.splitlines() if line.strip())
    return lines


def pdf_page_images(path: str | Path) -> list[bytes]:
    """Cada página como PNG, para pasarla por el OCR."""
    import io

    import pypdfium2 as pdfium

    images: list[bytes] = []
    document = pdfium.PdfDocument(str(path))
    try:
        for index in range(min(len(document), MAX_PAGES)):
            image = document[index].render(scale=_RENDER_SCALE).to_pil()
            out = io.BytesIO()
            image.convert("RGB").save(out, format="PNG")
            images.append(out.getvalue())
    finally:
        document.close()
    return images


def pdf_table_rows(path: str | Path) -> list[list[str]]:
    """Las filas de las tablas del PDF, página tras página, como una sola tabla.

    Primero con las líneas dibujadas de la tabla; si no las tiene, alineando el
    texto en columnas. Si la cabecera se repite en cada página, sólo cuenta la
    primera vez.
    """
    import pdfplumber

    rows: list[list[str]] = []
    header: list[str] | None = None
    starts: list[float] | None = None
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            # Con líneas dibujadas, las de pdfplumber; sin ellas, alineando las
            # palabras con la cabecera (su modo «text» recortaba la última letra
            # de algunas celdas: «ID Cliente» salía «ID Client»).
            tables = page.extract_tables()
            if not tables:
                aligned, starts = _aligned_rows(page, starts)
                tables = [aligned]
            for table in tables:
                for raw in table:
                    row = [" ".join(str(cell or "").split()) for cell in raw]
                    if not any(row):
                        continue
                    if header is None:
                        header = row
                    elif row == header:
                        continue
                    rows.append(row)
    return rows


_ROW_TOLERANCE = 3.0      # puntos: palabras a esta altura son la misma fila
_COLUMN_GAP = 12.0        # puntos: un hueco mayor entre palabras separa columnas


def _aligned_rows(page, starts: list[float] | None) -> tuple[list[list[str]], list[float] | None]:
    """Una tabla sin líneas: filas por altura y columnas por dónde empiezan las
    palabras de la cabecera. Las columnas se calculan con la primera fila de la
    primera página y se reutilizan en las siguientes, repitan o no la cabecera."""
    words = sorted(page.extract_words(), key=lambda w: (w["top"], w["x0"]))
    lines: list[list[dict]] = []
    for word in words:
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= _ROW_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    if not lines:
        return [], starts
    if starts is None:
        first = sorted(lines[0], key=lambda w: w["x0"])
        starts = [first[0]["x0"]]
        for prev, word in zip(first, first[1:]):
            if word["x0"] - prev["x1"] > _COLUMN_GAP:
                starts.append(word["x0"])
    table = []
    for line in lines:
        cells = [""] * len(starts)
        for word in sorted(line, key=lambda w: w["x0"]):
            column = max((i for i, x in enumerate(starts) if x <= word["x0"] + 5), default=0)
            cells[column] = f"{cells[column]} {word['text']}".strip()
        table.append(cells)
    return table, starts
