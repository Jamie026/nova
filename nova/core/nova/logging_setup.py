"""Logs estructurados: JSON a archivo (auditoría) + línea legible en consola."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .paths import logs_dir


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-5s  %(name)-18s  %(message)s", "%H:%M:%S"))
    root.addHandler(console)

    day = datetime.now().strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(logs_dir() / f"nova-{day}.jsonl", encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    for noisy in ("websockets", "openai", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def audit(logger: logging.Logger, event: str, **fields) -> None:
    """Traza de auditoría: toda ejecución de herramienta pasa por aquí."""
    logger.info(event, extra={"extra_fields": {"event": event, **fields}})
