"""Leer hojas de cálculo (Excel y CSV) como filas con nombre de columna.

Lo que se le pasa al modelo es un resumen (columnas y unas filas); el envío en
lote vuelve a leer el archivo entero por su cuenta, así que cientos de filas no
viajan por el contexto ni pueden llegar recortadas.
"""
from __future__ import annotations

import csv
import io
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

MAX_ROWS = 5000
EXTENSIONS = (".xlsx", ".xlsm", ".csv", ".txt", ".pdf")


@dataclass(slots=True)
class Table:
    columns: list[str]
    rows: list[dict[str, str]]          # una por fila de datos, por nombre de columna
    sheet: str = ""
    sheets: list[str] = field(default_factory=list)
    first_row: int = 2                  # número de fila (como en Excel) del primer dato
    truncated: bool = False


def normalize_key(text: str) -> str:
    """«Teléfono », «telefono» y «TELÉFONO» son la misma columna."""
    text = unicodedata.normalize("NFD", str(text).strip().lower())
    return " ".join("".join(c for c in text if unicodedata.category(c) != "Mn").split())


def find_column(table: Table, name: str) -> str | None:
    wanted = normalize_key(name)
    for column in table.columns:
        if normalize_key(column) == wanted:
            return column
    for column in table.columns:            # «tel» para «Teléfono móvil»
        if wanted and wanted in normalize_key(column):
            return column
    return None


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "sí" if value else "no"
    # Excel guarda los teléfonos como número: 987654321.0 tiene que salir sin «.0».
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.strftime("%d/%m/%Y %H:%M") if value.time() != datetime.min.time() \
            else value.strftime("%d/%m/%Y")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return str(value).strip()


def _from_grid(grid: list[list[str]], sheet: str, sheets: list[str]) -> Table:
    start = next((i for i, row in enumerate(grid) if any(row)), None)
    if start is None:
        return Table([], [], sheet, sheets)
    header = grid[start]
    columns: list[str] = []
    for index, name in enumerate(header):
        name = name or f"Columna {index + 1}"
        while name in columns:              # dos columnas con el mismo título
            name += " (2)"
        columns.append(name)
    rows: list[dict[str, str]] = []
    truncated = False
    for raw in grid[start + 1:]:
        if not any(raw):
            continue
        if len(rows) >= MAX_ROWS:
            truncated = True
            break
        padded = list(raw) + [""] * (len(columns) - len(raw))
        rows.append(dict(zip(columns, padded[:len(columns)])))
    return Table(columns, rows, sheet, sheets, start + 2, truncated)


def _read_excel(path: Path, sheet: str) -> Table:
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    try:
        names = list(book.sheetnames)
        chosen = sheet or names[0]
        if chosen not in names:
            match = [n for n in names if normalize_key(n) == normalize_key(chosen)]
            if not match:
                raise ValueError(f"No hay ninguna hoja «{sheet}». Hojas: {', '.join(names)}")
            chosen = match[0]
        grid = [[_cell(v) for v in row]
                for row in book[chosen].iter_rows(max_row=MAX_ROWS + 50, values_only=True)]
    finally:
        book.close()
    return _from_grid(grid, chosen, names)


def _read_csv(path: Path) -> Table:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
        # Excel en español guarda el CSV con «;».
        if sample.count(";") > sample.count(","):
            dialect.delimiter = ";"
    grid = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text), dialect)]
    return _from_grid(grid, "", [])


def _read_pdf(path: Path) -> Table:
    from .pdf import pdf_table_rows

    rows = pdf_table_rows(path)
    if len(rows) < 2:
        raise ValueError(f"No encontré ninguna tabla en {path.name}. Si es un PDF escaneado (una "
                         "foto de la lista), pásalo a Excel o CSV: una tabla escaneada no se puede "
                         "leer con seguridad.")
    return _from_grid(rows, "", [])


def read_table(path: str | Path, sheet: str = "") -> Table:
    target = Path(path).expanduser()
    suffix = target.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return _read_excel(target, sheet)
    if suffix in (".csv", ".txt"):
        return _read_csv(target)
    if suffix == ".pdf":
        return _read_pdf(target)
    if suffix == ".xls":
        raise ValueError("Es un Excel antiguo (.xls). Ábrelo en Excel y guárdalo como .xlsx")
    raise ValueError(f"No sé leer tablas de archivos {suffix or 'sin extensión'}")


def preview(table: Table, limit: int = 15) -> str:
    """Las primeras filas, con su número de fila de Excel."""
    lines = [" | ".join(table.columns)]
    for offset, row in enumerate(table.rows[:limit]):
        number = table.first_row + offset
        lines.append(f"{number}: " + " | ".join(row.get(c, "") for c in table.columns))
    if len(table.rows) > limit:
        lines.append(f"… y {len(table.rows) - limit} filas más")
    return "\n".join(lines)
