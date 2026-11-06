"""Planificar un envío de WhatsApp en lote: teléfonos, mensajes y plazos.

Aquí no se toca la pantalla: se decide a quién, qué y cuándo, y se lleva la
cuenta de lo enviado en disco. Esa cuenta es la que evita mandar dos veces el
mismo mensaje si Nova se cierra a medias y se vuelve a pedir el envío.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..files.tables import Table, find_column, normalize_key

_FIELD = re.compile(r"\{([^{}]+)\}")
DAY_S = 24 * 3600


@dataclass(slots=True)
class Message:
    row: int | str      # fila de la hoja (o archivo de la factura), para que el usuario la encuentre
    phone: str          # sólo dígitos, con código de país
    text: str
    key: str = ""       # lo que se apunta como enviado; vacío = el teléfono
    attachment: str = ""  # archivo que se envía (el texto va como comentario)

    @property
    def label(self) -> str:
        return f"fila {self.row}" if isinstance(self.row, int) else str(self.row)


@dataclass(slots=True)
class Plan:
    messages: list[Message] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    already_sent: int = 0


def normalize_phone(raw: str, country_code: str = "") -> str | None:
    """Dígitos con código de país, o None si no puede ser un móvil.

    «+51 987 654 321», «0051987654321» y «987654321» con country_code=51 dan
    «51987654321». Sin código de país y sin prefijo, un número corto no se
    adivina: sería mandar el mensaje a otro país.
    """
    text = str(raw).strip()
    if not text:
        return None
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if plus:
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif len(digits) <= 10 or digits.startswith("0"):   # «0…»: prefijo nacional, no de país
        code = re.sub(r"\D", "", country_code)
        if not code:
            return None
        digits = code + digits.lstrip("0")
    return digits if 8 <= len(digits) <= 15 else None


def display_phone(phone: str) -> str:
    return f"+{phone}"


def template_fields(template: str) -> list[str]:
    return list(dict.fromkeys(m.strip() for m in _FIELD.findall(template)))


def needs_country_code(table: Table, phone_column: str) -> bool:
    column = find_column(table, phone_column)
    return column is not None and any(
        normalize_phone(r.get(column, "")) is None and normalize_phone(r.get(column, ""), "1")
        for r in table.rows)


def plan(table: Table, phone_column: str, template: str, country_code: str = "",
         sent: set[str] | None = None) -> Plan:
    """Lanza ValueError si la columna o algún campo de la plantilla no existe."""
    column = find_column(table, phone_column)
    if column is None:
        raise ValueError(f"No hay ninguna columna «{phone_column}». Columnas: {', '.join(table.columns)}")
    fields = {name: find_column(table, name) for name in template_fields(template)}
    missing = [name for name, found in fields.items() if found is None]
    if missing:
        raise ValueError(f"La plantilla usa {', '.join('{' + m + '}' for m in missing)}, que no "
                         f"son columnas. Columnas: {', '.join(table.columns)}")
    if not template.strip():
        raise ValueError("El mensaje está vacío")

    result = Plan()
    seen: set[str] = set()
    for offset, row in enumerate(table.rows):
        number = table.first_row + offset
        phone = normalize_phone(row.get(column, ""), country_code)
        if phone is None:
            value = row.get(column, "")
            result.skipped.append((number, f"teléfono no válido «{value}»" if value else "sin teléfono"))
            continue
        if phone in seen:
            result.skipped.append((number, f"{display_phone(phone)} repetido"))
            continue
        seen.add(phone)
        empty = [name for name, col in fields.items() if not row.get(col, "").strip()]
        if empty:
            result.skipped.append((number, f"{', '.join(empty)} vacío"))
            continue
        if sent and phone in sent:
            result.already_sent += 1
            continue
        text = _FIELD.sub(lambda m: row.get(fields[m.group(1).strip()], ""), template)
        result.messages.append(Message(number, phone, text.strip()))
    return result


def schedule_minutes(count: int, batch_size: int, pause_min: float, gap_s: float) -> float:
    """Lo que tardará, más o menos: los mensajes de cada tanda y las pausas entre tandas."""
    if count <= 0:
        return 0.0
    batches = -(-count // batch_size)
    return (count * gap_s) / 60 + (batches - 1) * pause_min


class Progress:
    """Lo enviado por cada envío en lote, en disco, y el historial del día.

    `key` identifica el envío: el mismo archivo, la misma columna y el mismo
    mensaje. Volver a pedirlo salta lo que ya salió.
    """

    def __init__(self, folder: Path, source: str, phone_column: str, template: str):
        self._folder = folder
        digest = hashlib.sha1(
            f"{Path(source).resolve()}|{normalize_key(phone_column)}|{template}".encode()).hexdigest()[:16]
        self.path = folder / f"{digest}.json"
        self._history = folder / "_historial.json"
        self.data = self._load(self.path, {"source": str(source), "template": template,
                                           "sent": {}, "failed": {}})

    @staticmethod
    def _load(path: Path, default):
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self, path: Path, data) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)

    @property
    def sent(self) -> set[str]:
        return set(self.data["sent"])

    def mark(self, phone: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.data["sent"][phone] = time.time()
            self.data["failed"].pop(phone, None)
            history = [t for t in self._load(self._history, []) if time.time() - t < DAY_S]
            history.append(time.time())
            self._save(self._history, history)
        else:
            self.data["failed"][phone] = detail
        self._save(self.path, self.data)

    def sent_last_day(self) -> list[float]:
        return sorted(t for t in self._load(self._history, []) if time.time() - t < DAY_S)
