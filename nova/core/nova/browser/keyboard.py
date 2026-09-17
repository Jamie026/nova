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
import time
import logging
from dataclasses import dataclass

from ..os_adapter.base import OsAdapter, WindowInfo

log = logging.getLogger("nova.browser")

# Sufijos de título con los que cada navegador firma sus ventanas.
BROWSER_SIGNATURES = (" - opera", " - google chrome", " - microsoft edge", " - brave",
                      " - mozilla firefox", " - vivaldi", " - chromium")
# El título no siempre lleva la firma: Opera GX titula la ventana sólo con la
# página en algunas versiones, y una pestaña con título largo se ve truncada.
# El proceso no miente.
BROWSER_PROCESSES = ("opera", "chrome", "msedge", "brave", "firefox", "vivaldi", "chromium")
# Qué se lanza cuando no hay ninguno abierto y el usuario tiene preferencia.
BROWSER_APP_NAMES = {"opera": "Opera GX", "chrome": "Google Chrome", "edge": "Microsoft Edge",
                     "brave": "Brave", "firefox": "Firefox", "vivaldi": "Vivaldi"}
SETTLE_S = 0.35
PAGE_LOAD_S = 1.2
_TITLE_WAIT_S = 4.0
_EMPTY_TAB_TITLES = {"acceso rápido", "new tab", "nueva pestaña", "speed dial", "página nueva", "start page"}


@dataclass(slots=True)
class PageInfo:
    title: str
    window: str


def find_browser_window(os_adapter: OsAdapter, prefer: str = "") -> WindowInfo | None:
    """La ventana de navegador que el usuario tiene abierta, si hay alguna.

    Si hay varias gana la que está en primer plano, y si el usuario ha dicho
    cuál prefiere («opera»), esa.
    """
    candidates = [w for w in os_adapter.list_windows() if _is_browser(w)]
    if not candidates:
        return None
    if prefer:
        wanted = [w for w in candidates if _matches(w, prefer)]
        if wanted:
            candidates = wanted
    # Una minimizada vale (focus_window la restaura), pero detrás de las visibles.
    candidates.sort(key=lambda w: (w.minimized, not w.foreground))
    return candidates[0]


def _is_browser(window: WindowInfo) -> bool:
    title = window.title.lower()
    process = (window.process or "").lower().removesuffix(".exe")
    return any(title.endswith(sig) for sig in BROWSER_SIGNATURES) or process in BROWSER_PROCESSES


def _matches(window: WindowInfo, prefer: str) -> bool:
    prefer = prefer.lower()
    return prefer in window.title.lower() or prefer in (window.process or "").lower()


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

    async def ensure(self, timeout_s: float = 10.0) -> WindowInfo | None:
        """La ventana del navegador, lanzándolo si hace falta y hay preferido.

        Sin esto, con Opera cerrado Nova abría SU Chrome por CDP, y el
        usuario, que había dicho «en Opera», veía aparecer Chrome."""
        window = self.window()
        if window is not None or not self._prefer:
            return window
        app_name = BROWSER_APP_NAMES.get(self._prefer.lower(), self._prefer)
        app = self._os.find_app(app_name) or self._os.find_app(self._prefer)
        if app is None or not self._os.open_app(app):
            return None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            window = self.window()
            if window is not None:
                await asyncio.sleep(PAGE_LOAD_S)   # que termine de pintar
                return window
        return None

    async def _focus(self) -> WindowInfo:
        window = self.window()
        if window is None:
            raise RuntimeError("No hay ningún navegador abierto")
        if not self._os.focus_window(window.handle):
            raise RuntimeError(f"No pude traer {window.title} al frente")
        # Esperar a que esté DE VERDAD en primer plano: restaurar una ventana
        # minimizada lleva una animación de ~0,5 s, y las teclas enviadas
        # mientras tanto se pierden (pasó: «ww.youtube.com» sin la «https://w»).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            current = next((w for w in self._os.list_windows() if w.handle == window.handle), None)
            if current is not None and current.foreground and not current.minimized:
                break
        await asyncio.sleep(SETTLE_S)
        return window

    async def _enter_in_bar(self, text: str, new_tab: bool) -> None:
        """Pone `text` en la barra de direcciones y pulsa Enter.

        Pegado desde el portapapeles, no tecleado: el pegado es atómico y no
        pierde caracteres aunque la barra tarde en tomar el foco. Ctrl+A antes
        por si la barra no seleccionó lo que tenía. El portapapeles del usuario
        se deja como estaba."""
        combo = ["ctrl", "t"] if new_tab else ["ctrl", "l"]
        if not self._os.press_keys(combo):
            raise RuntimeError("No pude abrir la barra de direcciones")
        await asyncio.sleep(SETTLE_S)
        previous = ""
        pasted = False
        set_clipboard = getattr(self._os, "set_clipboard", None)
        if set_clipboard is not None:
            try:
                previous = self._os.get_clipboard()
            except Exception:
                pass
            try:
                if set_clipboard(text):
                    self._os.press_keys(["ctrl", "a"])
                    await asyncio.sleep(0.05)
                    pasted = self._os.press_keys(["ctrl", "v"])
                    await asyncio.sleep(0.15)
            except Exception as exc:
                log.debug("no pude pegar la dirección: %s", exc)
            finally:
                if previous:
                    try:
                        set_clipboard(previous)
                    except Exception:
                        pass
        if not pasted and not self._os.type_text(text):
            raise RuntimeError("No pude escribir en la barra de direcciones")
        await asyncio.sleep(0.1)
        self._os.press_keys(["enter"])

    async def _current(self, fallback: WindowInfo, before: str = "") -> PageInfo:
        """La página que hay ahora. Espera a que el título deje de ser el de
        antes o el de la pestaña vacía («Acceso Rápido», «New Tab»), hasta
        _TITLE_WAIT_S: si no, informaba «Abrí YouTube (Acceso Rápido)»."""
        deadline = time.monotonic() + _TITLE_WAIT_S
        window = self.window() or fallback
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            window = self.window() or fallback
            title = _strip_signature(window.title)
            if title and title != before and title.lower() not in _EMPTY_TAB_TITLES:
                break
        return PageInfo(_strip_signature(window.title), window.title)

    async def goto(self, url: str, new_tab: bool = False) -> PageInfo:
        window = await self._focus()
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        await self._enter_in_bar(url, new_tab)
        return await self._current(window, before=_strip_signature(window.title))

    async def search(self, query: str, new_tab: bool = False) -> PageInfo:
        # La barra de direcciones busca con el buscador que el usuario tenga
        # configurado en SU navegador, que es lo que espera que pase.
        window = await self._focus()
        await self._enter_in_bar(query, new_tab)
        return await self._current(window, before=_strip_signature(window.title))

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
