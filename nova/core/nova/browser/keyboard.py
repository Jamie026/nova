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
import re
import time
from dataclasses import dataclass
from typing import Callable

from ..os_adapter.base import OsAdapter, TabInfo, WindowInfo

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
NEW_TAB_SETTLE_S = 0.9
CLIPBOARD_HOLD_S = 0.6     # el portapapeles es nuestro hasta que la página ya se pidió
PAGE_LOAD_S = 1.2
_TITLE_WAIT_S = 4.0
_EMPTY_TAB_TITLES = {"acceso rápido", "new tab", "nueva pestaña", "speed dial", "página nueva", "start page"}


@dataclass(slots=True)
class PageInfo:
    title: str
    window: str
    new_tab: bool = False


# Pestañas que son una aplicación en uso y no deben reemplazarse por otra web.
_IN_USE = re.compile(r"whatsapp|gmail|outlook|google meet|^meet\b|zoom|teams|discord|telegram|slack|"
                     r"messenger|instagram|google docs|documentos de google|hojas de c[aá]lculo|sheets",
                     re.I)


def in_use_tab(title: str) -> bool:
    return bool(_IN_USE.search(title or ""))


@dataclass(slots=True)
class SiteVisit:
    window: WindowInfo
    title: str
    reused: bool        # True si ya había una pestaña de ese sitio


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

    def find_tab(self, matches: Callable[[str], bool]) -> tuple[WindowInfo, TabInfo] | None:
        """La pestaña que cumple `matches`, en cualquier ventana de navegador,
        leyendo la barra de pestañas por accesibilidad."""
        found, _blind = self._find_tab(matches)
        return found

    def _find_tab(self, matches: Callable[[str], bool]) -> tuple[tuple[WindowInfo, TabInfo] | None, list[WindowInfo]]:
        """Además de la pestaña, las ventanas cuya barra no se pudo leer. Opera
        deja de exponerla por accesibilidad en algunos estados (medido: con la
        ventana tapada por otra devolvía cero pestañas, minimizada sí las daba),
        y «cero pestañas» no es «no hay ninguna de WhatsApp»: es «no lo sé»."""
        windows = [w for w in self._os.list_windows() if _is_browser(w)]
        if self._prefer:
            windows.sort(key=lambda w: not _matches(w, self._prefer))
        blind: list[WindowInfo] = []
        for window in windows:
            tabs = self._os.browser_tabs(window.handle)
            if not tabs:
                blind.append(window)
            for tab in tabs:
                if matches(tab.title):
                    return (window, tab), blind
        return None, blind

    async def _scan_tabs(self, window: WindowInfo, matches: Callable[[str], bool]) -> str | None:
        """Sin accesibilidad: recorre las pestañas con Ctrl+1…Ctrl+8 leyendo el
        título de la ventana. Si no está, vuelve a la que estaba el usuario."""
        start = _strip_signature(window.title)
        seen: list[str] = []
        for number in range(1, 9):
            self._os.press_keys(["ctrl", str(number)])
            await asyncio.sleep(SETTLE_S)
            current = next((w for w in self._os.list_windows() if w.handle == window.handle), window)
            title = _strip_signature(current.title)
            if matches(title):
                return title
            if seen and title == seen[-1]:
                break                      # no hay más pestañas: Ctrl+N no cambió nada
            seen.append(title)
        if start in seen:
            self._os.press_keys(["ctrl", str(seen.index(start) + 1)])
            await asyncio.sleep(SETTLE_S)
        return None

    async def open_site(self, url: str, matches: Callable[[str], bool],
                        url_for_tab: Callable[[str], str | None] = lambda _title: None) -> SiteVisit:
        """Lleva al sitio sin duplicarlo: si ya hay una pestaña, la usa; si no,
        abre otra sin pisar la que el usuario estaba mirando. Sin navegador
        abierto lanza el del sistema, que es el que tiene sus sesiones.

        `url_for_tab(título)` dice adónde ir dentro de la pestaña reutilizada;
        None la deja donde está.
        """
        found, blind = self._find_tab(matches)
        if found is None and blind:
            # La barra no se pudo leer: se trae la ventana al frente y se mira de
            # nuevo; si sigue ciega, se recorren las pestañas a mano.
            await self._focus(blind[0])
            found, blind = self._find_tab(matches)
            if found is None and blind:
                title = await self._scan_tabs(blind[0], matches)
                if title is not None:
                    return await self._use_tab(blind[0], title, url_for_tab)
        if found is not None:
            window, tab = found
            await self._focus(window)
            if not tab.active and not self._os.select_tab(window.handle, tab.index):
                raise RuntimeError(f"No pude cambiar a la pestaña «{tab.title}»")
            await asyncio.sleep(SETTLE_S)
            return await self._use_tab(window, tab.title, url_for_tab)

        window = await self.ensure()
        if window is None:
            return await self._launch(url)
        await self._focus(window)
        await self._enter_in_bar(url, new_tab=True)
        page = await self._current(window, before=_strip_signature(window.title))
        return SiteVisit(self.window() or window, page.title, False)

    async def focus_tab(self, matches: Callable[[str], bool]) -> str | None:
        """Pasa a la pestaña que cumple `matches`, sin navegar. Devuelve su título,
        o None si no está (y entonces el usuario sigue en la que estaba)."""
        found, blind = self._find_tab(matches)
        if found is None and blind:
            await self._focus(blind[0])
            found, blind = self._find_tab(matches)
            if found is None and blind:
                return await self._scan_tabs(blind[0], matches)
        if found is None:
            return None
        window, tab = found
        await self._focus(window)
        if not tab.active and not self._os.select_tab(window.handle, tab.index):
            return None
        await asyncio.sleep(SETTLE_S)
        return tab.title

    async def switch_index(self, index: int) -> PageInfo:
        """Ctrl+1…Ctrl+8; 9 o más, la última (Ctrl+9 en todos los navegadores)."""
        window = await self._focus()
        before = _strip_signature(window.title)
        self._os.press_keys(["ctrl", str(min(index, 9))])
        return await self._current(window, before=before)

    async def list_tabs(self) -> list[TabInfo]:
        """Las pestañas de la ventana del navegador. Por accesibilidad si se
        puede; si no, recorriéndolas con Ctrl+número y volviendo a la del usuario."""
        window = self.window()
        if window is None:
            return []
        tabs = self._os.browser_tabs(window.handle)
        if tabs:
            return tabs
        window = await self._focus(window)
        start = _strip_signature(window.title)
        titles: list[str] = []
        for number in range(1, 9):
            self._os.press_keys(["ctrl", str(number)])
            await asyncio.sleep(SETTLE_S)
            current = next((w for w in self._os.list_windows() if w.handle == window.handle), window)
            title = _strip_signature(current.title)
            if titles and title == titles[-1]:
                break
            titles.append(title)
        if start in titles:
            self._os.press_keys(["ctrl", str(titles.index(start) + 1)])
            await asyncio.sleep(SETTLE_S)
        return [TabInfo(i, t, t == start) for i, t in enumerate(titles)]

    async def _use_tab(self, window: WindowInfo, title: str,
                       url_for_tab: Callable[[str], str | None]) -> SiteVisit:
        target = url_for_tab(title)
        if target is None:
            return SiteVisit(window, title, True)
        await self._enter_in_bar(target, new_tab=False)
        page = await self._current(window, before=title)
        return SiteVisit(window, page.title, True)

    async def _launch(self, url: str, timeout_s: float = 15.0) -> SiteVisit:
        if not self._os.open_path(url):
            raise RuntimeError("No pude abrir el navegador")
        deadline = time.monotonic() + timeout_s
        while True:
            window = self.window()
            if window is not None:
                await asyncio.sleep(PAGE_LOAD_S)
                window = self.window() or window
                return SiteVisit(window, _strip_signature(window.title), False)
            if time.monotonic() >= deadline:
                raise RuntimeError("El navegador no llegó a abrirse")
            await asyncio.sleep(0.5)

    async def _focus(self, window: WindowInfo | None = None) -> WindowInfo:
        window = window or self.window()
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
        # Una pestaña nueva tarda más en tener la barra lista que Ctrl+L.
        await asyncio.sleep(NEW_TAB_SETTLE_S if new_tab else SETTLE_S)
        set_clipboard = getattr(self._os, "set_clipboard", None)
        previous = ""
        ours = False
        try:
            if set_clipboard is not None:
                try:
                    previous = self._os.get_clipboard()
                    ours = bool(set_clipboard(text)) and self._os.get_clipboard() == text
                except Exception as exc:
                    log.debug("no pude usar el portapapeles: %s", exc)
            pasted = False
            if ours:
                self._os.press_keys(["ctrl", "a"])
                await asyncio.sleep(0.05)
                pasted = self._os.press_keys(["ctrl", "v"])
                await asyncio.sleep(0.25)
            if not pasted and not self._os.type_text(text):
                raise RuntimeError("No pude escribir en la barra de direcciones")
            await asyncio.sleep(0.1)
            # Opera y Chrome completan lo escrito con algo del historial y lo dejan
            # seleccionado: «https://www.youtube.com» + «/send?phone=51…» de una
            # visita a WhatsApp, y Enter abría eso (pasó: un 404 de YouTube). Supr
            # borra esa sugerencia; si no hay ninguna, no hace nada.
            self._os.press_keys(["delete"])
            await asyncio.sleep(0.05)
            self._os.press_keys(["enter"])
            await asyncio.sleep(CLIPBOARD_HOLD_S)
        finally:
            # El portapapeles del usuario vuelve DESPUÉS de Enter, y sólo si sigue
            # teniendo lo nuestro. Antes volvía justo tras Ctrl+V: con una pestaña
            # nueva la barra pegó tarde, pegó lo del usuario (un log que había
            # copiado) y Enter lo buscó en Google.
            if ours and previous:
                try:
                    if self._os.get_clipboard() == text:
                        set_clipboard(previous)
                except Exception:
                    pass

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
        # Las páginas internas del navegador («about:blank», «opera://settings»)
        # no llevan https: con él, Opera lo buscaba en Google («https://about:blank»).
        if not re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            url = "https://" + url
        # Una pestaña que el usuario está usando como aplicación (WhatsApp,
        # Gmail, una reunión) no se pisa: «abre YouTube» con WhatsApp delante
        # lo abre al lado. Pasó: YouTube reemplazó a WhatsApp.
        replaced = _strip_signature(window.title)
        if not new_tab and in_use_tab(replaced):
            new_tab = True
        await self._enter_in_bar(url, new_tab)
        page = await self._current(window, before=replaced)
        page.new_tab = new_tab
        return page

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

    async def new_tab(self) -> PageInfo:
        """Una pestaña vacía: sólo Ctrl+T, sin escribir nada en la barra."""
        window = await self._focus()
        self._os.press_keys(["ctrl", "t"])
        await asyncio.sleep(NEW_TAB_SETTLE_S)
        current = self.window() or window
        return PageInfo(_strip_signature(current.title) or "pestaña nueva", current.title, new_tab=True)

    async def close_tab(self) -> PageInfo:
        window = await self._focus()
        self._os.press_keys(["ctrl", "w"])
        return await self._current(window)

    async def next_tab(self) -> PageInfo:
        window = await self._focus()
        self._os.press_keys(["ctrl", "tab"])
        return await self._current(window)
