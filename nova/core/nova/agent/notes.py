"""Lo que Nova sabe del usuario y no debe olvidar entre sesiones.

La memoria de conversación dura lo que dura la sesión, y las «preferencias» que
había hasta ahora eran tres campos fijos (navegador, apps frecuentes, volumen).
Nada de eso guarda «uso Opera, no Chrome», «ábreme las páginas en la pestaña
actual», «Alain es mi hermano», «llámame Jamie». Esas cosas las dice el usuario
una vez, y a la siguiente sesión Nova las había olvidado.

Esto es una lista de frases cortas en un JSON. El modelo las lee en cada turno,
en el prompt, y las añade o las quita con dos herramientas. Es deliberadamente
simple: sin embeddings, sin base de datos, sin resumen automático. Sesenta frases
de una línea caben en el prompt sin que se note, y con más de sesenta el
problema no es el almacenamiento sino que el usuario ya no sabe qué le ha dicho.

No se guarda nada sin que el modelo lo decida explícitamente, y el modelo tiene
la instrucción de guardar preferencias y hechos estables, no la conversación.
"""
from __future__ import annotations

import json
import logging
import time
import unicodedata
from pathlib import Path
from typing import Any

log = logging.getLogger("nova.notes")

MAX_NOTES = 60


def _key(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(text.split())


class UserNotes:
    def __init__(self, path: Path):
        self._path = path
        self._notes: list[dict[str, Any]] = []
        self.load()

    # ------------------------------------------------------------ persistencia
    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text("utf-8"))
            self._notes = [n for n in data.get("notes", []) if isinstance(n, dict) and n.get("text")]
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("memoria ilegible (%s); empiezo vacía sin borrarla", exc)

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"notes": self._notes}, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
        except OSError as exc:
            log.warning("no pude guardar la memoria: %s", exc)

    # --------------------------------------------------------------- contenido
    def add(self, text: str) -> bool:
        text = " ".join(text.strip().split())
        if not text or len(text) > 200:
            return False
        key = _key(text)
        for note in self._notes:
            if _key(note["text"]) == key:
                note["updated"] = time.time()
                self.save()
                return False               # ya estaba
        self._notes.append({"text": text, "created": time.time(), "updated": time.time()})
        if len(self._notes) > MAX_NOTES:
            # Se descarta la que lleva más tiempo sin refrescarse, no la más vieja:
            # una preferencia antigua que se sigue usando no debe caerse.
            self._notes.sort(key=lambda n: n.get("updated", 0))
            self._notes = self._notes[-MAX_NOTES:]
        self.save()
        return True

    def remove(self, text: str) -> int:
        """Quita las notas que contengan ese texto. Devuelve cuántas."""
        key = _key(text)
        if not key:
            return 0
        before = len(self._notes)
        self._notes = [n for n in self._notes if key not in _key(n["text"])]
        removed = before - len(self._notes)
        if removed:
            self.save()
        return removed

    def all(self) -> list[str]:
        return [n["text"] for n in self._notes]

    def render(self) -> str:
        if not self._notes:
            return "(nada todavía)"
        return "\n".join(f"- {n['text']}" for n in self._notes)
