"""Emparejar lo que dice el usuario con una ventana abierta.

Vive aparte porque lo necesitan tanto las capturas como el control directo, y
porque es la clase de heurística que se ajusta a mano tras usarla: el título de
una ventana de Discord es «#general | Mi servidor - Discord», y el usuario dice
«Discord». Comparar sólo por título fallaría; comparar también con el nombre del
proceso resuelve la mayoría de los casos reales.
"""
from __future__ import annotations

from difflib import SequenceMatcher

from ..os_adapter.base import OsAdapter, WindowInfo

MIN_SCORE = 0.55


def score_window(window: WindowInfo, query: str) -> float:
    target = query.lower().strip()
    if not target:
        return 0.0
    title = window.title.lower()
    process = window.process.lower().removesuffix(".exe")
    if process == target:
        return 1.0
    if target in process or process.startswith(target):
        return 0.95
    if target in title:
        return 0.90
    return max(
        SequenceMatcher(None, title, target).ratio(),
        SequenceMatcher(None, process, target).ratio(),
    )


def best_window(os_adapter: OsAdapter, query: str) -> WindowInfo | None:
    best, best_score = None, 0.0
    for window in os_adapter.list_windows():
        score = score_window(window, query)
        # A igualdad, gana la que ya está en primer plano: es a la que se refiere
        # el usuario cuando dice «esta ventana» o cuando hay dos de la misma app.
        if score > best_score or (score == best_score and window.foreground):
            best, best_score = window, score
    return best if best_score >= MIN_SCORE else None
