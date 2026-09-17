"""Tres memorias con vidas distintas.

* ConversationMemory — turnos de la sesión actual, en RAM. Es lo que permite
  entender "ábrelo", "el anterior", "ahora búscalo en inglés".
* ActionLog — últimas herramientas ejecutadas con su resultado, también en RAM.
  Se inyecta en el prompt: sin esto el modelo no sabe a qué se refiere "ciérralo".
* Preferences — disco. Sobrevive a reinicios.

No se guarda audio nunca. Las transcripciones sólo persisten si el usuario activa
memoria de larga duración (fuera del MVP).
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..paths import preferences_file

log = logging.getLogger("nova.memory")


@dataclass(slots=True)
class ActionRecord:
    tool: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    at: float = field(default_factory=time.time)

    def render(self) -> str:
        mark = "✓" if self.ok else "✗"
        args = ", ".join(f"{k}={v!r}" for k, v in list(self.arguments.items())[:3])
        return f"{mark} {self.tool}({args}) → {self.summary[:120]}"


class ConversationMemory:
    def __init__(self, max_turns: int = 24):
        self._messages: deque[dict[str, Any]] = deque(maxlen=max_turns * 3)

    def add(self, message: dict[str, Any]) -> None:
        self._messages.append(message)

    def add_user(self, text: str) -> None:
        self.add({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        if text:
            self.add({"role": "assistant", "content": text})

    def messages(self) -> list[dict[str, Any]]:
        """Devuelve el historial sin dejar tool_calls huérfanos al recortar.

        La API rechaza un mensaje `tool` cuyo `assistant` con tool_calls se cayó
        del buffer, así que se descartan esos huérfanos del principio.
        """
        items = list(self._messages)
        pending: set[str] = set()
        cleaned: list[dict[str, Any]] = []
        for message in items:
            if message.get("role") == "tool":
                if message.get("tool_call_id") not in pending:
                    continue
            if message.get("role") == "assistant" and message.get("tool_calls"):
                pending = {c["id"] for c in message["tool_calls"]}
            cleaned.append(message)
        return cleaned

    def clear(self) -> None:
        self._messages.clear()


class ActionLog:
    def __init__(self, max_actions: int = 20):
        self._records: deque[ActionRecord] = deque(maxlen=max_actions)

    def add(self, record: ActionRecord) -> None:
        self._records.append(record)

    def recent(self, count: int = 6) -> list[ActionRecord]:
        return list(self._records)[-count:]

    def render(self, count: int = 6) -> str:
        records = self.recent(count)
        if not records:
            return "(ninguna todavía)"
        return "\n".join(r.render() for r in records)

    def clear(self) -> None:
        self._records.clear()


class Preferences:
    DEFAULTS: dict[str, Any] = {
        "preferred_browser": "chrome",
        "frequent_apps": [],
        "usual_volume": 45,
        # La posición del avatar NO vive aquí: es un asunto de ventana y la
        # guarda Electron en window-state.json. Tener dos sitios donde se
        # recuerda lo mismo termina siempre en que uno de los dos miente.
    }

    def __init__(self) -> None:
        self._data = dict(self.DEFAULTS)
        self.load()

    def load(self) -> None:
        path = preferences_file()
        if path.exists():
            try:
                self._data.update(json.loads(path.read_text("utf-8")))
            except (OSError, json.JSONDecodeError):
                log.warning("preferencias ilegibles; uso los valores por defecto")

    def save(self) -> None:
        try:
            preferences_file().write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("no pude guardar preferencias: %s", exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    def note_app_use(self, name: str) -> None:
        apps: list[str] = [a for a in self._data.get("frequent_apps", []) if a != name]
        apps.insert(0, name)
        self._data["frequent_apps"] = apps[:10]
        self.save()

    def render(self) -> str:
        apps = ", ".join(a for a in self._data.get("frequent_apps", [])[:5] if a) or "sin datos"
        return f"apps frecuentes: {apps}"
