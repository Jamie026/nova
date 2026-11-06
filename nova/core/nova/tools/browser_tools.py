"""Herramientas de navegador expuestas al modelo."""
from __future__ import annotations

from ..browser.controller import BrowserController
from ..browser.keyboard import KeyboardBrowser
from ..os_adapter.base import OsAdapter
from .registry import Risk, Tool, ToolResult

# Qué herramientas necesitan CDP sí o sí. Con el navegador del usuario por
# teclado no se puede leer el DOM ni pulsar por rol; para eso están
# read_screen y click_item, que leen la pantalla.
_CDP_ONLY_HINT = ("Esa acción necesita control profundo del navegador y estás usando el "
                  "navegador que ya tenías abierto. Para leer lo que hay usa read_screen; "
                  "para pulsar algo, click_item. O pon browser.channel = \"chrome\" para "
                  "que Nova lance su propio Chrome controlable.")


def _plain(text: str) -> str:
    table = str.maketrans("áéíóúüñ", "aeiouun")
    return " ".join(str(text).lower().translate(table).split())


def _tabs_summary(tabs) -> str:
    if not tabs:
        return "No hay pestañas abiertas"
    return " | ".join(f"{t.index + 1}. {t.title[:45]}{' ←' if t.active else ''}" for t in tabs[:12])


def build(browser: BrowserController, os_adapter: OsAdapter | None = None,
          mode: str = "auto", prefer: str = "") -> list[Tool]:
    """`mode`: "auto" usa el navegador que el usuario tenga abierto (por teclado)
    y sólo si no hay ninguno lanza Chrome por CDP; "keyboard" siempre el del
    usuario; "cdp" siempre Chrome propio."""
    keyboard = KeyboardBrowser(os_adapter, prefer) if os_adapter is not None else None

    async def _use_keyboard() -> bool:
        if keyboard is None or mode == "cdp":
            return False
        if mode == "keyboard":
            return True
        if keyboard.window() is not None:
            return True
        # Sin navegador abierto: si hay uno preferido, se lanza ése. Sólo si
        # no hay preferido (o no arranca) se cae al Chrome propio por CDP.
        return prefer != "" and await keyboard.ensure() is not None

    def _cdp_only() -> ToolResult:
        return ToolResult(False, _CDP_ONLY_HINT)

    async def browser_open() -> ToolResult:
        if await _use_keyboard():
            window = keyboard.window()
            return ToolResult(True, f"Uso el navegador que ya tienes abierto: {window.title}",
                              {"mode": "keyboard", "window": window.title})
        await browser.ensure()
        tabs = await browser.list_tabs()
        return ToolResult(True, f"Navegador listo. {_tabs_summary(tabs)}", {"tabs": len(tabs)})

    async def browser_search(query: str, new_tab: bool = False) -> ToolResult:
        if await _use_keyboard():
            page = await keyboard.search(query, new_tab=new_tab)
            return ToolResult(True, f"Busqué «{query}» en tu navegador. Página actual: {page.title}")
        tab = await browser.search(query, new_tab=new_tab)
        return ToolResult(True, f"Busqué «{query}». Página actual: {tab.title}", {"url": tab.url})

    async def browser_goto(url: str) -> ToolResult:
        if await _use_keyboard():
            page = await keyboard.goto(url)
            where = ("en una pestaña nueva, para no cerrarte la que tenías abierta" if page.new_tab
                     else "en la pestaña actual de tu navegador")
            return ToolResult(True, f"Abrí {url} {where} ({page.title})", {"new_tab": page.new_tab})
        tab = await browser.goto(url)
        return ToolResult(True, f"Abrí {tab.title} ({tab.url})", {"url": tab.url})

    async def browser_new_tab(url: str = "") -> ToolResult:
        if await _use_keyboard():
            if not url:
                page = await keyboard.new_tab()
            else:
                page = await keyboard.goto(url, new_tab=True)
            return ToolResult(True, f"Pestaña nueva en tu navegador: {page.title or 'en blanco'}")
        tab = await browser.new_tab(url or None)
        return ToolResult(True, f"Pestaña nueva: {tab.title or 'en blanco'}", {"url": tab.url})

    async def browser_close_tab(index: int | None = None) -> ToolResult:
        if await _use_keyboard():
            if index is not None:
                await keyboard.switch_index(index)
            page = await keyboard.close_tab()
            return ToolResult(True, f"Cerré la pestaña. Ahora: {page.title}")
        zero_based = None if index is None else index - 1
        ok = await browser.close_tab(zero_based)
        return ToolResult(ok, "Pestaña cerrada" if ok else "Esa pestaña no existe")

    async def browser_switch_tab(index: int | None = None, match: str = "") -> ToolResult:
        if await _use_keyboard():
            if match:
                wanted = _plain(match)
                title = await keyboard.focus_tab(lambda t: wanted in _plain(t))
                if title is None:
                    return ToolResult(False, f"No hay ninguna pestaña abierta de «{match}». Si el usuario "
                                             "quiere abrirla, usa browser_goto.")
                return ToolResult(True, f"Pasé a la pestaña «{title}»")
            if index is not None:
                page = await keyboard.switch_index(index)
                return ToolResult(True, f"Pasé a la pestaña {index}: {page.title}")
            page = await keyboard.next_tab()
            return ToolResult(True, f"Pasé a la siguiente pestaña: {page.title}")
        tab = await browser.switch_tab(None if index is None else index - 1, match or None)
        if tab is None:
            return ToolResult(False, "No encontré esa pestaña")
        return ToolResult(True, f"Cambié a «{tab.title}»", {"url": tab.url})

    async def browser_list_tabs() -> ToolResult:
        if await _use_keyboard():
            tabs = await keyboard.list_tabs()
            if not tabs:
                return ToolResult(False, "No pude ver las pestañas del navegador")
            listing = " | ".join(f"{t.index + 1}. {t.title[:45]}{' ←' if t.active else ''}" for t in tabs)
            return ToolResult(True, listing, {"tabs": [{"index": t.index + 1, "title": t.title,
                                                       "active": t.active} for t in tabs]})
        tabs = await browser.list_tabs()
        data = [{"index": t.index + 1, "title": t.title, "url": t.url, "active": t.active} for t in tabs]
        return ToolResult(True, _tabs_summary(tabs), {"tabs": data})

    async def browser_click(text: str) -> ToolResult:
        if await _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.click(text))

    async def browser_open_result(position: int) -> ToolResult:
        if await _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.click_result(position))

    async def browser_type(text: str, into: str = "", submit: bool = False) -> ToolResult:
        if await _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.type_text(text, into or None, submit))

    async def browser_read_page() -> ToolResult:
        if await _use_keyboard():
            return _cdp_only()
        content = await browser.read_page()
        return ToolResult(True, content or "La página no tiene texto legible", {"text": content})

    async def browser_find_in_page(text: str) -> ToolResult:
        if await _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.find_in_page(text))

    async def browser_scroll(direction: str = "down", amount: int = 600) -> ToolResult:
        if await _use_keyboard():
            await keyboard._focus()
            os_adapter.press_keys(["pagedown" if direction == "down" else "pageup"])
            return ToolResult(True, f"Desplacé la página hacia {'abajo' if direction == 'down' else 'arriba'}")
        ok = await browser.scroll(direction, amount)
        return ToolResult(ok, f"Desplacé la página hacia {'abajo' if direction == 'down' else 'arriba'}")

    async def browser_go_back() -> ToolResult:
        if await _use_keyboard():
            page = await keyboard.go_back()
            return ToolResult(True, f"Volví atrás. Ahora: {page.title}")
        ok = await browser.go_back()
        return ToolResult(ok, "Volví a la página anterior" if ok else "No hay historial hacia atrás")

    no_args = {"type": "object", "properties": {}, "additionalProperties": False}

    return [
        Tool("browser_open", "Abre o conecta con el navegador. Úsala antes de cualquier otra acción web si aún no hay navegador.",
             no_args, browser_open, Risk.LOW, 40),
        Tool("browser_search", "Hace una búsqueda web con el buscador configurado.",
             {"type": "object",
              "properties": {"query": {"type": "string"}, "new_tab": {"type": "boolean", "default": False}},
              "required": ["query"], "additionalProperties": False},
             browser_search, Risk.LOW, 40),
        Tool("browser_goto", "Navega a una URL en la pestaña activa del navegador que el usuario tiene abierto "
                             "(Opera, Chrome, Edge...). Para 'abre YouTube' basta con esto, sin browser_open ni pestaña nueva.",
             {"type": "object", "properties": {"url": {"type": "string"}},
              "required": ["url"], "additionalProperties": False},
             browser_goto, Risk.LOW, 40),
        Tool("browser_new_tab", "Abre una pestaña nueva, opcionalmente en una URL.",
             {"type": "object", "properties": {"url": {"type": "string"}}, "additionalProperties": False},
             browser_new_tab, Risk.LOW, 40),
        Tool("browser_close_tab", "Cierra una pestaña por su número (empezando en 1). Sin número cierra la activa.",
             {"type": "object", "properties": {"index": {"type": "integer", "minimum": 1}},
              "additionalProperties": False},
             browser_close_tab, Risk.LOW, 20),
        Tool("browser_switch_tab", "Cambia de pestaña por número o por texto de su título o URL.",
             {"type": "object",
              "properties": {"index": {"type": "integer", "minimum": 1}, "match": {"type": "string"}},
              "additionalProperties": False},
             browser_switch_tab, Risk.LOW, 20),
        Tool("browser_list_tabs", "Lista las pestañas abiertas con su número, título y URL.",
             no_args, browser_list_tabs, Risk.LOW, 20),
        Tool("browser_click", "Hace clic en un enlace, botón o control identificado por su texto visible o etiqueta accesible.",
             {"type": "object", "properties": {"text": {"type": "string"}},
              "required": ["text"], "additionalProperties": False},
             browser_click, Risk.LOW, 30),
        Tool("browser_open_result", "Abre el enésimo resultado de una página de resultados de búsqueda (1 = el primero).",
             {"type": "object", "properties": {"position": {"type": "integer", "minimum": 1, "maximum": 20}},
              "required": ["position"], "additionalProperties": False},
             browser_open_result, Risk.LOW, 40),
        Tool("browser_type", "Escribe texto en un campo de la página. 'into' es la etiqueta o placeholder del campo.",
             {"type": "object",
              "properties": {"text": {"type": "string"}, "into": {"type": "string"},
                             "submit": {"type": "boolean", "default": False}},
              "required": ["text"], "additionalProperties": False},
             browser_type, Risk.MEDIUM, 30,
             "¿Escribo «{text}» en la página actual?"),
        Tool("browser_read_page", "Lee el texto principal de la pestaña activa. Úsala para responder sobre lo que hay en pantalla.",
             no_args, browser_read_page, Risk.LOW, 30),
        Tool("browser_find_in_page", "Busca un texto dentro de la página activa y devuelve el fragmento donde aparece.",
             {"type": "object", "properties": {"text": {"type": "string"}},
              "required": ["text"], "additionalProperties": False},
             browser_find_in_page, Risk.LOW, 30),
        Tool("browser_scroll", "Desplaza la página activa.",
             {"type": "object",
              "properties": {"direction": {"type": "string", "enum": ["up", "down"]},
                             "amount": {"type": "integer", "minimum": 100, "maximum": 3000}},
              "additionalProperties": False},
             browser_scroll, Risk.LOW, 15),
        Tool("browser_go_back", "Vuelve a la página anterior del historial.",
             no_args, browser_go_back, Risk.LOW, 20),
    ]
