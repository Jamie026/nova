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

    def _use_keyboard() -> bool:
        if keyboard is None or mode == "cdp":
            return False
        if mode == "keyboard":
            return True
        return keyboard.window() is not None

    def _cdp_only() -> ToolResult:
        return ToolResult(False, _CDP_ONLY_HINT)

    async def browser_open() -> ToolResult:
        if _use_keyboard():
            window = keyboard.window()
            return ToolResult(True, f"Uso el navegador que ya tienes abierto: {window.title}",
                              {"mode": "keyboard", "window": window.title})
        await browser.ensure()
        tabs = await browser.list_tabs()
        return ToolResult(True, f"Navegador listo. {_tabs_summary(tabs)}", {"tabs": len(tabs)})

    async def browser_search(query: str, new_tab: bool = False) -> ToolResult:
        if _use_keyboard():
            page = await keyboard.search(query, new_tab=new_tab)
            return ToolResult(True, f"Busqué «{query}» en tu navegador. Página actual: {page.title}")
        tab = await browser.search(query, new_tab=new_tab)
        return ToolResult(True, f"Busqué «{query}». Página actual: {tab.title}", {"url": tab.url})

    async def browser_goto(url: str) -> ToolResult:
        if _use_keyboard():
            page = await keyboard.goto(url)
            return ToolResult(True, f"Abrí {url} en la pestaña actual de tu navegador ({page.title})")
        tab = await browser.goto(url)
        return ToolResult(True, f"Abrí {tab.title} ({tab.url})", {"url": tab.url})

    async def browser_new_tab(url: str = "") -> ToolResult:
        if _use_keyboard():
            if not url:
                page = await keyboard.goto("about:blank", new_tab=True)
            else:
                page = await keyboard.goto(url, new_tab=True)
            return ToolResult(True, f"Pestaña nueva en tu navegador: {page.title or 'en blanco'}")
        tab = await browser.new_tab(url or None)
        return ToolResult(True, f"Pestaña nueva: {tab.title or 'en blanco'}", {"url": tab.url})

    async def browser_close_tab(index: int | None = None) -> ToolResult:
        if _use_keyboard():
            if index is not None:
                return ToolResult(False, "En tu navegador sólo puedo cerrar la pestaña activa (Ctrl+W); "
                                         "cambia a ella primero.")
            page = await keyboard.close_tab()
            return ToolResult(True, f"Cerré la pestaña. Ahora: {page.title}")
        zero_based = None if index is None else index - 1
        ok = await browser.close_tab(zero_based)
        return ToolResult(ok, "Pestaña cerrada" if ok else "Esa pestaña no existe")

    async def browser_switch_tab(index: int | None = None, match: str = "") -> ToolResult:
        if _use_keyboard():
            page = await keyboard.next_tab()
            return ToolResult(True, f"Pasé a la siguiente pestaña: {page.title}")
        tab = await browser.switch_tab(None if index is None else index - 1, match or None)
        if tab is None:
            return ToolResult(False, "No encontré esa pestaña")
        return ToolResult(True, f"Cambié a «{tab.title}»", {"url": tab.url})

    async def browser_list_tabs() -> ToolResult:
        if _use_keyboard():
            return _cdp_only()
        tabs = await browser.list_tabs()
        data = [{"index": t.index + 1, "title": t.title, "url": t.url, "active": t.active} for t in tabs]
        return ToolResult(True, _tabs_summary(tabs), {"tabs": data})

    async def browser_click(text: str) -> ToolResult:
        if _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.click(text))

    async def browser_open_result(position: int) -> ToolResult:
        if _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.click_result(position))

    async def browser_type(text: str, into: str = "", submit: bool = False) -> ToolResult:
        if _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.type_text(text, into or None, submit))

    async def browser_read_page() -> ToolResult:
        if _use_keyboard():
            return _cdp_only()
        content = await browser.read_page()
        return ToolResult(True, content or "La página no tiene texto legible", {"text": content})

    async def browser_find_in_page(text: str) -> ToolResult:
        if _use_keyboard():
            return _cdp_only()
        return ToolResult(True, await browser.find_in_page(text))

    async def browser_scroll(direction: str = "down", amount: int = 600) -> ToolResult:
        if _use_keyboard():
            await keyboard._focus()
            os_adapter.press_keys(["pagedown" if direction == "down" else "pageup"])
            return ToolResult(True, f"Desplacé la página hacia {'abajo' if direction == 'down' else 'arriba'}")
        ok = await browser.scroll(direction, amount)
        return ToolResult(ok, f"Desplacé la página hacia {'abajo' if direction == 'down' else 'arriba'}")

    async def browser_go_back() -> ToolResult:
        if _use_keyboard():
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
