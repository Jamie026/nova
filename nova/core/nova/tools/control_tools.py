"""Control directo del escritorio: teclado, ratón y la receta de «adjunta esto ahí».

Esta es la parte peligrosa del agente y está escrita como tal:

* **Todo es riesgo alto.** Ninguna de estas herramientas se ejecuta sin que el
  usuario confirme, y el riesgo alto no es configurable (ver ToolRegistry). Un
  teclado que escribe solo puede mandar un mensaje a quien no era, aceptar un
  diálogo que no habías leído o escribir dentro de tu banco.
* **El texto de confirmación dice lo que va a pasar, literalmente.** Incluye el
  mensaje que se va a escribir y si se va a pulsar Enter. Confirmar a ciegas no
  es confirmar.
* **Las pulsaciones se limitan a una lista blanca de teclas** con nombre. El
  modelo no puede enviar códigos crudos ni cadenas arbitrarias de scancodes.
* **El ratón sólo hace clic donde ya sabemos que hay algo.** Se expone
  `click_at` porque a veces no hay alternativa, pero el prompt empuja al modelo a
  usar el navegador por rol accesible y las apps por ventana, no por píxeles: las
  coordenadas se rompen al cambiar de resolución o de tema.
* **`NOVA_NO_INPUT=1` desactiva la inyección de entrada** sin tocar la
  configuración. Es el freno de mano para desarrollar y para las demos.

El último recurso siempre disponible: Ctrl+Shift+X aborta lo que Nova esté
haciendo, y mover el ratón o teclear tú mismo gana la carrera porque estas
herramientas no bloquean la entrada real.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from ..files.search import FileSearcher
from ..os_adapter.base import OsAdapter
from .matching import best_window
from .registry import Risk, Tool, ToolResult

# Teclas que el modelo puede nombrar. Cualquier otra cosa se rechaza antes de llegar al SO.
ALLOWED_KEYS = {
    "ctrl", "control", "shift", "alt", "win", "windows",
    "enter", "return", "tab", "esc", "escape", "space", "backspace", "delete", "supr",
    "insert", "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
    *(f"f{n}" for n in range(1, 13)),
    *(chr(c) for c in range(ord("a"), ord("z") + 1)),
    *(str(d) for d in range(10)),
}

MAX_TYPE_CHARS = 1500
# Tiempo que tarda una app de Electron (Discord, Slack) en mostrar el adjunto pegado.
PASTE_SETTLE_S = 0.9
FOCUS_SETTLE_S = 0.45
# Lo que tarda el «quick switcher» en rellenar resultados tras escribir un nombre.
# Menos de esto y el Enter cae sobre una lista todavía vacía.
SWITCHER_SETTLE_S = 0.8
CHAT_OPEN_SETTLE_S = 0.6

# Cómo se salta a un chat por nombre en cada aplicación. Todas las de esta lista
# abren un buscador con Ctrl+K que acepta el nombre de una persona o canal y lo
# abre con Enter, dejando el cursor en la caja de mensaje. Es la única forma de
# navegar por Discord sin ver la pantalla: sin árbol de accesibilidad útil y sin
# modelo de visión, el teclado es lo que hay, y este atajo es estable desde hace
# años en las tres.
CHAT_SWITCHERS: dict[str, list[str]] = {
    "discord": ["ctrl", "k"],
    "slack": ["ctrl", "k"],
    "telegram": ["ctrl", "k"],
}

# Títulos de ventana de Discord que NO son una conversación. Teclear ahí no envía
# nada a nadie: pasó de verdad («hola» + Enter en «Amigos - Discord», y la
# herramienta lo dio por enviado porque las teclas salieron). El título de la
# ventana es la única señal que tenemos de dónde estamos, y es fiable: Discord
# pone el nombre del chat delante del guion.
_NOT_A_CHAT = ("amigos", "friends", "descubrir", "discover", "ajustes", "settings",
               "nitro", "tienda", "shop", "mensajes directos", "direct messages")


def looks_like_chat(title: str, app: str) -> bool:
    """¿Este título de ventana corresponde a una conversación abierta?"""
    head = title.split(" - ")[0].strip().lower() if " - " in title else title.strip().lower()
    if not head or head == app.lower():
        return False              # «Discord» a secas: la pantalla de inicio
    return not any(head.startswith(page) for page in _NOT_A_CHAT)


def _normalize_name(text: str) -> str:
    table = str.maketrans("áéíóúüñ", "aeiouun")
    return " ".join(text.lower().translate(table).split())


def title_matches_chat(title: str, chat: str) -> bool:
    """¿El título nombra al chat que pedimos? Tolera apodos: basta una palabra."""
    head = _normalize_name(title.split(" - ")[0]) if " - " in title else _normalize_name(title)
    wanted = [w for w in _normalize_name(chat).split() if len(w) >= 3]
    return any(w in head for w in wanted) if wanted else False


def input_enabled() -> bool:
    return os.environ.get("NOVA_NO_INPUT", "").lower() not in ("1", "true", "yes")


def validate_combo(keys: list[str]) -> list[str]:
    """Normaliza y valida una combinación; lanza ValueError si hay algo raro."""
    if not keys:
        raise ValueError("no indicaste ninguna tecla")
    if len(keys) > 4:
        raise ValueError("demasiadas teclas a la vez")
    clean = [str(k).strip().lower() for k in keys]
    unknown = [k for k in clean if k not in ALLOWED_KEYS]
    if unknown:
        raise ValueError(f"teclas no permitidas: {', '.join(unknown)}")
    return clean


def build(os_adapter: OsAdapter, searcher: FileSearcher) -> list[Tool]:
    def _blocked() -> ToolResult:
        return ToolResult(False, "El control directo del teclado está desactivado (NOVA_NO_INPUT=1)")

    async def focus_app(name: str) -> ToolResult:
        window = best_window(os_adapter, name)
        if window is None:
            return ToolResult(False, f"No hay ninguna ventana abierta que se parezca a «{name}»")
        ok = os_adapter.focus_window(window.handle)
        await asyncio.sleep(FOCUS_SETTLE_S)
        return ToolResult(ok, f"Traje al frente {window.title}" if ok
                          else f"No pude enfocar {window.title}",
                          {"handle": window.handle, "title": window.title})

    async def type_text(text: str, press_enter: bool = False) -> ToolResult:
        if not input_enabled():
            return _blocked()
        if len(text) > MAX_TYPE_CHARS:
            return ToolResult(False, f"El texto pasa de {MAX_TYPE_CHARS} caracteres")
        ok = os_adapter.type_text(text)
        if ok and press_enter:
            await asyncio.sleep(0.12)
            ok = os_adapter.press_keys(["enter"])
        preview = text if len(text) <= 60 else text[:57] + "…"
        return ToolResult(ok, f"Escribí «{preview}»" if ok else "No pude escribir el texto")

    async def press_hotkey(keys: list[str], repeat: int = 1) -> ToolResult:
        if not input_enabled():
            return _blocked()
        try:
            combo = validate_combo(keys)
        except ValueError as exc:
            return ToolResult(False, str(exc))
        ok = True
        for _ in range(max(1, min(repeat, 10))):
            ok = os_adapter.press_keys(combo) and ok
            await asyncio.sleep(0.05)
        return ToolResult(ok, f"Pulsé {'+'.join(combo)}" if ok else "No pude enviar la combinación")

    async def click_at(x: int, y: int, button: str = "left", double: bool = False) -> ToolResult:
        if not input_enabled():
            return _blocked()
        width, height = os_adapter.screen_size()
        if width and height and not (0 <= x < width and 0 <= y < height):
            return ToolResult(False, f"({x}, {y}) cae fuera de la pantalla de {width}x{height}")
        ok = os_adapter.click(x, y, button=button, double=double)
        return ToolResult(ok, f"Clic en ({x}, {y})" if ok else "No pude hacer clic")

    def _switcher_for(title: str, app: str) -> list[str] | None:
        haystack = f"{title} {app}".lower()
        for key, combo in CHAT_SWITCHERS.items():
            if key in haystack:
                return combo
        return None

    async def _open_chat_in(window, app: str, chat: str) -> ToolResult:
        combo = _switcher_for(window.title, app)
        if combo is None:
            return ToolResult(False, f"No sé cómo abrir chats por nombre en «{app}». "
                                     f"Sé hacerlo en: {', '.join(CHAT_SWITCHERS)}.")
        if not os_adapter.focus_window(window.handle):
            return ToolResult(False, f"No pude traer {window.title} al frente")
        await asyncio.sleep(FOCUS_SETTLE_S)
        if not os_adapter.press_keys(combo):
            return ToolResult(False, "No pude abrir el buscador de chats")
        await asyncio.sleep(0.35)
        if not os_adapter.type_text(chat):
            return ToolResult(False, "No pude escribir el nombre del chat")
        await asyncio.sleep(SWITCHER_SETTLE_S)
        if not os_adapter.press_keys(["enter"]):
            return ToolResult(False, "No pude confirmar el chat")
        await asyncio.sleep(CHAT_OPEN_SETTLE_S)

        # Comprobar, no suponer. El buscador puede no encontrar a nadie con ese
        # nombre, y entonces Enter no hace nada y seguimos donde estábamos. Si se
        # diera por abierto, el siguiente paso teclearía el mensaje en la pantalla
        # de amigos y diría que lo envió.
        after = best_window(os_adapter, app)
        title = after.title if after else window.title
        if title_matches_chat(title, chat):
            return ToolResult(True, f"Abrí el chat «{chat}» ({title})",
                              {"window": title, "chat": chat})
        if looks_like_chat(title, app):
            return ToolResult(True, f"Abrí una conversación, pero el título dice «{title}» "
                                    f"y no «{chat}»: comprueba que sea la correcta",
                              {"window": title, "chat": chat, "uncertain": True})
        return ToolResult(False, f"No encontré ningún chat llamado «{chat}»: la ventana sigue "
                                 f"en «{title}». Pídele al usuario el nombre exacto.")

    async def open_chat(app: str, chat: str) -> ToolResult:
        """Salta a un chat por nombre usando el buscador rápido de la app."""
        if not input_enabled():
            return _blocked()
        window = best_window(os_adapter, app)
        if window is None:
            return ToolResult(False, f"No hay ninguna ventana de «{app}» abierta. Ábrela primero.")
        return await _open_chat_in(window, app, chat)

    async def send_message(app: str, text: str, chat: str = "",
                           send: bool = True) -> ToolResult:
        """(Abrir el chat) → escribir → (Enter). Una herramienta, una confirmación.

        Como con send_file_to_app: la coreografía va junta porque enviar no se
        deshace. Si el modelo tuviera que encadenar open_chat + type_text +
        press_hotkey, la confirmación del Enter llegaría separada del texto, y
        el usuario tendría que recordar qué estaba a punto de enviarse. Aquí la
        pregunta lleva el mensaje, el destinatario y la app en una sola frase.
        """
        if not input_enabled():
            return _blocked()
        if len(text) > MAX_TYPE_CHARS:
            return ToolResult(False, f"El mensaje pasa de {MAX_TYPE_CHARS} caracteres")
        window = best_window(os_adapter, app)
        if window is None:
            return ToolResult(False, f"No hay ninguna ventana de «{app}» abierta. Ábrela primero.")

        title = window.title
        if chat:
            opened = await _open_chat_in(window, app, chat)
            if not opened.ok:
                return opened
            title = opened.data.get("window", title)
        else:
            if not looks_like_chat(window.title, app):
                return ToolResult(False, f"No hay ninguna conversación abierta en {app} (la "
                                         f"ventana está en «{window.title}»). Dime a quién "
                                         "va el mensaje y la abro.")
            if not os_adapter.focus_window(window.handle):
                return ToolResult(False, f"No pude traer {window.title} al frente")
            await asyncio.sleep(FOCUS_SETTLE_S)

        if not os_adapter.type_text(text):
            return ToolResult(False, "No pude escribir el mensaje")
        if send:
            await asyncio.sleep(0.15)
            if not os_adapter.press_keys(["enter"]):
                return ToolResult(False, "Escribí el mensaje pero no pude enviarlo")

        preview = text if len(text) <= 60 else text[:57] + "…"
        destino = f" a «{chat}»" if chat else ""
        detail = "y lo envié" if send else "y lo dejé escrito sin enviar"
        return ToolResult(True, f"Escribí «{preview}»{destino} en {title} {detail}",
                          {"window": title, "chat": chat, "sent": send})

    async def send_file_to_app(app: str, path: str, message: str = "",
                               send: bool = False) -> ToolResult:
        """Copiar → enfocar → pegar → (escribir) → (enviar).

        Existe como herramienta única y no como cuatro llamadas encadenadas
        porque los tiempos importan: entre pegar y escribir hay que esperar a que
        la aplicación procese el adjunto. Dejar esa coreografía en manos del
        modelo significa que a veces el mensaje se envía antes de que la imagen
        haya terminado de adjuntarse, y eso no se puede deshacer.
        """
        if not input_enabled():
            return _blocked()
        if not searcher.allows(path):
            return ToolResult(False, f"«{path}» está fuera de las carpetas permitidas")
        target = Path(path).expanduser()
        if not target.is_file():
            return ToolResult(False, f"No encuentro el archivo «{path}»")

        window = best_window(os_adapter, app)
        if window is None:
            return ToolResult(False, f"No hay ninguna ventana de «{app}» abierta. Ábrela primero.")

        if not os_adapter.copy_files_to_clipboard([str(target)]):
            return ToolResult(False, f"No pude copiar {target.name} al portapapeles")

        if not os_adapter.focus_window(window.handle):
            return ToolResult(False, f"No pude traer {window.title} al frente")
        await asyncio.sleep(FOCUS_SETTLE_S)

        if not os_adapter.press_keys(["ctrl", "v"]):
            return ToolResult(False, "No pude pegar en la ventana")
        await asyncio.sleep(PASTE_SETTLE_S)

        if message:
            os_adapter.type_text(message)
            await asyncio.sleep(0.15)

        if send:
            os_adapter.press_keys(["enter"])

        detail = "y lo envié" if send else "y lo dejé listo para que lo revises"
        return ToolResult(True, f"Pegué {target.name} en {window.title} {detail}",
                          {"path": str(target), "window": window.title, "sent": send})

    return [
        Tool(
            name="open_chat",
            description=(
                "Abre un chat, canal o conversación por el nombre de la persona o del canal "
                "dentro de una app de mensajería ya abierta (Discord, Slack, Telegram). Usa el "
                "buscador rápido de la app, así que no hace falta ver la pantalla."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Discord, Slack o Telegram"},
                    "chat": {"type": "string", "description": "Nombre de la persona, grupo o canal"},
                },
                "required": ["app", "chat"], "additionalProperties": False,
            },
            # Riesgo medio y no alto: escribe un nombre en un buscador y pulsa
            # Enter. Si se equivoca de chat, se abre otro; no se pierde nada.
            handler=open_chat, risk=Risk.MEDIUM, timeout_s=20,
            confirm_template="¿Abro el chat «{chat}» en {app}?",
        ),
        Tool(
            name="send_message",
            description=(
                "Escribe un mensaje en una app de mensajería y lo envía. Si indicas `chat`, "
                "primero abre esa conversación por nombre (Discord, Slack, Telegram). Es LA "
                "forma de mandar un mensaje a alguien: no encadenes open_chat + type_text + "
                "press_hotkey. Deja send=false sólo si el usuario quiere revisarlo antes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string"},
                    "text": {"type": "string", "maxLength": MAX_TYPE_CHARS},
                    "chat": {"type": "string",
                             "description": "Destinatario o canal; vacío = el chat que ya está abierto"},
                    "send": {"type": "boolean", "default": True},
                },
                "required": ["app", "text"], "additionalProperties": False,
            },
            handler=send_message, risk=Risk.HIGH, timeout_s=30,
            always_confirm=lambda a: bool(a.get("send", True)),
            confirm_template=lambda args: (
                ("¿Envío «{text}»{to} en {app}?" if args.get("send", True)
                 else "¿Escribo «{text}»{to} en {app} sin enviarlo?").format(
                    text=args.get("text", ""),
                    to=f" a «{args['chat']}»" if args.get("chat") else "",
                    app=args.get("app", "la app"))
            ),
        ),
        Tool(
            name="focus_app",
            description=(
                "Trae al frente la ventana de una aplicación por su nombre. Hazlo antes de "
                "escribir o pulsar teclas: el teclado va siempre a la ventana enfocada."
            ),
            parameters={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"], "additionalProperties": False,
            },
            handler=focus_app, risk=Risk.MEDIUM, timeout_s=12,
            confirm_template="¿Traigo «{name}» al frente?",
        ),
        Tool(
            name="type_text",
            description=(
                "Escribe texto con el teclado en la ventana que está enfocada, como si lo "
                "tecleara el usuario. Para escribir en una aplicación concreta, enfócala antes "
                "con focus_app."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": MAX_TYPE_CHARS},
                    "press_enter": {"type": "boolean", "default": False},
                },
                "required": ["text"], "additionalProperties": False,
            },
            handler=type_text, risk=Risk.HIGH, timeout_s=30,
            confirm_template="¿Escribo «{text}» en la ventana activa?",
        ),
        Tool(
            name="press_hotkey",
            description=(
                "Pulsa una combinación de teclas en la ventana enfocada, por ejemplo "
                "['ctrl','v'] para pegar o ['alt','tab'] para cambiar de ventana."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"},
                             "minItems": 1, "maxItems": 4},
                    "repeat": {"type": "integer", "minimum": 1, "maximum": 10, "default": 1},
                },
                "required": ["keys"], "additionalProperties": False,
            },
            handler=press_hotkey, risk=Risk.HIGH, timeout_s=15,
            confirm_template="¿Pulso {keys} en la ventana activa?",
        ),
        Tool(
            name="click_at",
            description=(
                "Hace clic en unas coordenadas absolutas de pantalla. Úsala sólo como último "
                "recurso: para el navegador usa browser_click, y para las aplicaciones usa "
                "focus_app y atajos de teclado, que no se rompen al cambiar la resolución."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
                    "double": {"type": "boolean", "default": False},
                },
                "required": ["x", "y"], "additionalProperties": False,
            },
            handler=click_at, risk=Risk.HIGH, timeout_s=12,
            confirm_template="¿Hago clic en ({x}, {y})?",
        ),
        Tool(
            name="send_file_to_app",
            description=(
                "Adjunta un archivo en una aplicación abierta: lo copia al portapapeles, enfoca "
                "la ventana, lo pega y opcionalmente escribe un mensaje y lo envía. Es la forma "
                "correcta de mandar una imagen por Discord, WhatsApp Web o Slack. Consigue la "
                "ruta antes con find_files o take_screenshot. Deja send=false si el usuario no "
                "pidió explícitamente enviarlo."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Nombre de la aplicación o ventana destino"},
                    "path": {"type": "string", "description": "Ruta del archivo a adjuntar"},
                    "message": {"type": "string", "description": "Texto opcional a escribir junto al archivo"},
                    "send": {"type": "boolean", "default": False,
                             "description": "true pulsa Enter y lo envía"},
                },
                "required": ["app", "path"], "additionalProperties": False,
            },
            handler=send_file_to_app, risk=Risk.HIGH, timeout_s=45,
            always_confirm=lambda a: bool(a.get("send", False)),
            # La frase cambia según lo irreversible que sea: enviar un mensaje no
            # se deshace, dejarlo escrito sí.
            confirm_template=lambda args: (
                "¿Pego «{name}» en {app}{msg} y lo envío?" if args.get("send")
                else "¿Pego «{name}» en {app}{msg} sin enviarlo todavía?"
            ).format(
                name=Path(args.get("path", "")).name or args.get("path", ""),
                app=args.get("app", "la app"),
                msg=f" con el mensaje «{args['message']}»" if args.get("message") else "",
            ),
        ),
    ]
