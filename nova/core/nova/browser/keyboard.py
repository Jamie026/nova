"""Navegar con el navegador que el usuario ya tiene abierto, por teclado.

El controlador por CDP hace muchas más cosas (leer la página, pulsar por rol,
escribir en campos), pero tiene un precio que en uso real resultó inaceptable:
necesita lanzar SU PROPIA ventana de Chrome con un perfil aparte. El usuario
tenía Opera GX abierto con sus sesiones y sus pestañas, pidió «abre YouTube» y
lo que quería era eso: YouTube en la pestaña que estaba mirando. En vez de eso
aparecía —o no— otra ventana de otro navegador. Y en cuanto cerró esa ventana,
todo lo demás falló con «Target page, context or browser has been closed».

Un navegador que ya está corriendo no expone el puerto de depuración, así que no
hay forma de engancharse a él por CDP. Lo que sí hay son los atajos, que llevan
veinte años siendo los mismos en Opera, Chrome, Edge, Brave y Firefox:

    Ctrl+L   barra de direcciones (escribir una URL o una búsqueda)
    Ctrl+T   pestaña nueva
    Ctrl+W   cerrar pestaña
    Alt+←    atrás
    Ctrl+Tab siguiente pestaña

Con eso se cubre lo que se pide por voz el 90 % de las veces. Lo que no se cubre
(leer la página, pulsar un botón por su nombre) lo cubre read_screen + click_item.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..os_adapter.base import OsAdapter, WindowInfo

log = logging.getLogger("nova.browser")

# Sufijos de título con los que cada navegador firma sus ventanas.
BROWSER_SIGNATURES = (" - opera", " - google chrome", " - microsoft edge", " - brave",
                      " - mozilla firefox", " - vivaldi", " - chromium")
SETTLE_S = 0.35
PAGE_LOAD_S = 1.2


@dataclass(slots=True)
class PageInfo:
    title: str
    window: str


def find_browser_window(os_adapter: OsAdapter, prefer: str = "") -> WindowInfo | None:
    """La ventana de navegador que el usuario tiene abierta, si hay alguna.

    Si hay varias gana la que está en primer plano, y si el usuario ha dicho
    cuál prefiere («opera»), esa.
    """
    candidates = [w for w in os_adapter.list_windows()
                  if any(w.title.lower().endswith(sig) for sig in BROWSER_SIGNATURES)
                  and not w.minimized]
    if not candidates:
        return None
    if prefer:
        wanted = [w for w in candidates if prefer.lower() in w.title.lower()]
        if wanted:
            candidates = wanted
    candidates.sort(key=lambda w: not w.foreground)
    return candidates[0]


def _strip_signature(title: str) -> str:
    low = title.lower()
    for sig in BROWSER_SIGNATURES:
        if low.endswith(sig):
            return title[: -len(sig)]
    return title


class KeyboardBrowser:
    def __init__(self, os_adapter: OsAdapter, prefer: str = ""):
        self._os = os_adapter
        self._prefer = prefer

    def window(self) -> WindowInfo | None:
        return find_browser_window(self._os, self._prefer)

    async def _focus(self) -> WindowInfo:
        window = self.window()
        if window is None:
            raise RuntimeError("No hay ningún navegador abierto")
        if not self._os.focus_window(window.handle):
            raise RuntimeError(f"No pude traer {window.title} al frente")
        await asyncio.sleep(SETTLE_S)
        return window

    async def _current(self, fallback: WindowInfo) -> PageInfo:
        await asyncio.sleep(PAGE_LOAD_S)
        window = self.window() or fallback
        return PageInfo(_strip_signature(window.title), window.title)

    async def goto(self, url: str, new_tab: bool = False) -> PageInfo:
        window = await self._focus()
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        combo = ["ctrl", "t"] if new_tab else ["ctrl", "l"]
        if not self._os.press_keys(combo):
            raise RuntimeError("No pude abrir la barra de direcciones")
        await asyncio.sleep(SETTLE_S)
        if not self._os.type_text(url):
            raise RuntimeError("No pude escribir la dirección")
        await asyncio.sleep(0.1)
        self._os.press_keys(["enter"])
        return await self._current(window)

    async def search(self, query: str, new_tab: bool = False) -> PageInfo:
        # La barra de direcciones busca con el buscador que el usuario tenga
        # configurado en SU navegador, que es lo que espera que pase.
        window = await self._focus()
        combo = ["ctrl", "t"] if new_tab else ["ctrl", "l"]
        self._os.press_keys(combo)
        await asyncio.sleep(SETTLE_S)
        if not self._os.type_text(query):
            raise RuntimeError("No pude escribir la búsqueda")
        await asyncio.sleep(0.1)
        self._os.press_keys(["enter"])
        return await self._current(window)

    async def go_back(self) -> PageInfo:
        window = await self._focus()
        self._os.press_keys(["alt", "left"])
        return await self._current(window)

    async def close_tab(self) -> PageInfo:
        window = await self._focus()
        self._os.press_keys(["ctrl", "w"])
        return await self._current(window)

    async def next_tab(self) -> PageInfo:
        window = await self._focus()
        self._os.press_keys(["ctrl", "tab"])
        return await self._current(window)
