"""Correo por Gmail y mensajes por WhatsApp.

Las dos cosas van en dos pasos: dejar el texto listo y enviarlo.

* **compose_email** abre la redacción de Gmail con destinatario, asunto y texto
  ya puestos. **email_send** lo envía, y pregunta siempre.
* **whatsapp_draft** abre el chat y deja el mensaje escrito, sin pulsar Enter.
  **whatsapp_send** lo envía, y pregunta siempre. La pregunta nombra al contacto
  que se LEYÓ en pantalla, no al que se pidió: si dijo «Ana» y se abrió «Ana
  María», eso es lo que tiene que ver antes de decir que sí.

Los dos envíos comprueban en pantalla que lo preparado sigue ahí.

WhatsApp Web no expone nada por accesibilidad ni tiene un atajo de búsqueda que
sirva en un teclado español (Ctrl+Alt es AltGr), y el título de la ventana no dice
qué chat está abierto. Lo único que dice dónde estamos es la pantalla, así que
todo se hace leyéndola con OCR y haciendo clic en lo leído. Si algo no cuadra se
para y se dice: un mensaje que no sale se repite; uno mandado a quien no era, no
se deshace.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlencode

from ..agent.spelling import is_email
from ..browser.keyboard import KeyboardBrowser
from ..capture import ocr
from ..capture.ocr import ScreenLine
from ..files.tables import read_table
from ..os_adapter.base import OsAdapter, WindowInfo
from . import invoices as inv
from .batch import DAY_S, Message, Progress, display_phone, normalize_phone, plan, schedule_minutes
from .control_tools import MAX_TYPE_CHARS, input_enabled
from .registry import Risk, Tool, ToolResult

log = logging.getLogger("nova.tools.messaging")

GMAIL_COMPOSE = "https://mail.google.com/mail/"
WHATSAPP_WEB = "https://web.whatsapp.com/"

_GMAIL_TAB = re.compile(r"(^|\s-\s)gmail$", re.I)
_WHATSAPP_TAB = re.compile(r"^(\(\d+\)\s*)?whatsapp(\s+web)?$", re.I)
_ACCOUNT = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_SIGN_IN = ("iniciar sesión", "inicia sesión", "sign in", "cuentas de google", "google accounts")

_SEARCH = re.compile(r"buscar|search", re.I)
_COMPOSER = re.compile(r"escribe un mensaje|type a message", re.I)
_SEND_BUTTON = re.compile(r"^(enviar|send)\b", re.I)
_MAIL_SENT = re.compile(r"mensaje enviado|message sent", re.I)
# El aviso de WhatsApp Web cuando el número no tiene WhatsApp o está mal.
_INVALID_PHONE = re.compile(r"no es v[aá]lido|isn.t valid|is not valid|invalid", re.I)
_PHONE_TIMEOUT_S = 60.0       # con Zoom compartiendo pantalla todo va más lento
_PREVIEW_TIMEOUT_S = 12.0      # lo que tarda en aparecer la vista previa de un archivo pegado
_UPLOAD_SETTLE_S = 3.0         # subir un PDF o una foto antes de comprobar que salió
_DISCARD = re.compile(r"^descartar$|^discard$", re.I)

# Envío en lote, «por plazos»: tandas con pausa, una espera al azar entre
# mensajes y un tope diario. WhatsApp bloquea cuentas personales que mandan
# muchos mensajes seguidos, sobre todo a quien no las tiene guardadas.
BATCH_DEFAULTS = {
    "country_code": "",        # p. ej. "51"; vacío = se pregunta si hace falta
    "batch_size": 20,
    "batch_pause_min": 15,
    "gap_s": [5, 5],           # entre mensajes, en envíos de hasta large_batch mensajes
    "gap_large_s": [10, 10],   # entre mensajes, en envíos más grandes
    "large_batch": 20,
    "daily_limit": 150,
    "warn_s": 10,              # aviso antes de cada tanda: Nova va a usar el ratón
}

# Estimado a partir de WhatsApp Web a 1920x1020, donde la caja de búsqueda está
# en y=243 y la barra del navegador acaba en y≈125: la columna de chats acaba en
# el ~32 % del ancho y el nombre del chat abierto debería quedar en esta franja
# por encima de la búsqueda. Si no se lee, whatsapp_draft para sin escribir.
_LEFT_PANEL = 0.32
_HEADER_ABOVE_SEARCH = (110, 20)
_COMPOSER_FROM = 0.6            # el mensaje escrito se busca en el 40 % inferior
_MIN_TEXT_MATCH = 0.6

_READY_TIMEOUT_S = 20.0         # lo que puede tardar en cargar WhatsApp Web
_APP_TIMEOUT_S = 15.0
_CLICK_SETTLE_S = 0.5
_SEARCH_SETTLE_S = 1.5
_CHAT_SETTLE_S = 1.0
_SEARCH_ROW_PX = 25            # alto de la fila de la caja de búsqueda
_SENT_SETTLE_S = 1.0
_MAIL_SENT_SETTLE_S = 1.5


def is_gmail_tab(title: str) -> bool:
    """«Recibidos (3) - ana@gmail.com - Gmail» sí; «Trucos de Gmail - YouTube», no."""
    return bool(_GMAIL_TAB.search(title.strip()))


def is_whatsapp_tab(title: str) -> bool:
    """«(1) WhatsApp» sí; «whatsapp - Buscar con Google», no."""
    return bool(_WHATSAPP_TAB.match(title.strip()))


def compose_url(to: str, subject: str = "", body: str = "", account: str = "") -> str:
    """Redacción a pantalla completa. Con la cuenta de la pestaña que se reutiliza:
    sin ella Gmail abriría la primera cuenta, que puede no ser la que estaba abierta."""
    query = {"view": "cm", "fs": "1", "to": to, "su": subject, "body": body}
    if account:
        query["authuser"] = account
    return f"{GMAIL_COMPOSE}?{urlencode(query)}"


def _account_in(title: str) -> str:
    match = _ACCOUNT.search(title)
    return match.group(0) if match else ""


def _one_line(text: str) -> str:
    """En WhatsApp un salto de línea es Enter: enviaría el mensaje antes de
    preguntar, porque type_text lo convierte en esa tecla."""
    return " ".join(text.split())


def _normalize(text: str) -> str:
    table = str.maketrans("áéíóúüñ", "aeiouun")
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower().translate(table)).split())


def _same_word(a: str, b: str) -> bool:
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def same_contact(line: str, chat: str) -> bool:
    """¿Esta línea empieza por el contacto pedido?

    Por palabras y desde el principio: «Ana» no vale para «Mariana» ni para un
    mensaje que diga «hola Ana». Vale tanto «Ana» para «Ana María» como «Juan
    Pérez» para un contacto guardado como «Juan». Tolera los errores del OCR.
    """
    words = _normalize(line).split()
    wanted = _normalize(chat).split()
    if not words or not wanted or not _same_word(words[0], wanted[0]):
        return False
    return all(any(_same_word(w, have) for have in words) for w in wanted[1:]) \
        or len(words) <= len(wanted)


def _first_line(text: str) -> str:
    return next((line for line in text.splitlines() if line.strip()), "")


def _text_in(lines: list[ScreenLine], text: str) -> ScreenLine | None:
    """La línea donde se lee el principio de `text`, tolerando errores del OCR."""
    wanted = _normalize(_first_line(text))[:20]
    for line in lines:
        have = _normalize(line.text)
        size = difflib.SequenceMatcher(None, wanted, have, autojunk=False) \
            .find_longest_match(0, len(wanted), 0, len(have)).size
        if size >= _MIN_TEXT_MATCH * len(wanted):
            return line
    return None


@dataclass(slots=True)
class _View:
    """Lo que se lee en la ventana, con la geometría de WhatsApp."""
    lines: list[ScreenLine]
    left: int
    top: int
    width: int
    height: int

    def _x(self, line: ScreenLine) -> float:
        return (line.center[0] - self.left) / self.width

    def _y(self, line: ScreenLine) -> float:
        return (line.center[1] - self.top) / self.height

    def search_box(self) -> ScreenLine | None:
        boxes = [l for l in self.lines if _SEARCH.search(l.text) and self._x(l) < 0.5]
        return boxes[0] if boxes else None

    def composer(self) -> ScreenLine | None:
        boxes = [l for l in self.lines if _COMPOSER.search(l.text) and self._x(l) >= _LEFT_PANEL]
        return boxes[-1] if boxes else None

    def results(self, chat: str, below: int) -> list[ScreenLine]:
        return [l for l in self.lines if self._x(l) < _LEFT_PANEL and l.center[1] > below
                and same_contact(l.text, chat)]

    def header(self, chat: str, search_y: int) -> ScreenLine | None:
        """El nombre del chat abierto, si es el pedido."""
        top, bottom = search_y - _HEADER_ABOVE_SEARCH[0], search_y - _HEADER_ABOVE_SEARCH[1]
        for line in self.lines:
            if self._x(line) >= _LEFT_PANEL and top <= line.center[1] <= bottom \
                    and same_contact(line.text, chat):
                return line
        return None

    def written(self, text: str) -> ScreenLine | None:
        """La línea donde se ve el mensaje escrito. Con varias líneas, la primera."""
        wanted = _normalize(_first_line(text))[:20]
        candidates = [l for l in self.lines if self._x(l) >= _LEFT_PANEL
                      and self._y(l) >= _COMPOSER_FROM]
        if not candidates:
            return None
        if not wanted:                       # sólo emojis: el OCR no los lee
            return candidates[-1]
        return _text_in(candidates, text)

    def chat_name(self) -> ScreenLine | None:
        """El nombre del chat abierto, sea cual sea (para abrirlo por número)."""
        box = self.search_box()
        if box is None:
            return None
        top = box.center[1] - _HEADER_ABOVE_SEARCH[0]
        bottom = box.center[1] - _HEADER_ABOVE_SEARCH[1]
        return next((l for l in self.lines if self._x(l) >= _LEFT_PANEL and len(l.text) < 40
                     and top <= l.center[1] <= bottom), None)

    def send_button(self) -> ScreenLine | None:
        """El de más abajo: un correo que empiece por «Enviar...» queda por encima."""
        buttons = [l for l in self.lines if len(l.text) < 14 and _SEND_BUTTON.match(l.text.strip())]
        return max(buttons, key=lambda l: l.y, default=None)


class InvalidPhone(RuntimeError):
    pass


Notify = Callable[..., Awaitable[None]]


async def _quiet(_text: str, speak: bool = False) -> None:
    return None


def build(os_adapter: OsAdapter, prefer: str = "", *, searcher=None, data_dir: Path | None = None,
          notify: Notify | None = None, is_idle: Callable[[], bool] | None = None,
          settings: dict | None = None, sender=None, llm_chat=None, read_text=None,
          on_activity=None) -> list[Tool]:
    """`notify(texto, speak=False)` avisa al usuario del avance de un lote;
    `is_idle()` dice si Nova está libre (sin turno en curso) para usar el ratón;
    `llm_chat(mensajes)` es el modelo, para sacar los datos de cada factura;
    `read_text(ruta) -> (líneas, confianzas)` lee una factura (imagen o PDF)."""
    keyboard = KeyboardBrowser(os_adapter, prefer)
    settings = {**BATCH_DEFAULTS, **(settings or {})}
    notify = notify or _quiet
    is_idle = is_idle or (lambda: True)

    async def _activity(active: bool) -> None:
        """Avisa a la sesión de que se está enviando, para que no se duerma."""
        if on_activity is not None:
            try:
                await on_activity(active)
            except Exception as exc:
                log.debug("no pude avisar de la actividad: %s", exc)
    folder = (data_dir or Path.home() / ".nova") / "envios"
    job: dict = {}
    # Lo que whatsapp_draft dejó escrito y whatsapp_send puede enviar.
    pending: dict = {}
    # Lo mismo para el correo.
    mail: dict = {}

    async def compose_email(to: str, subject: str = "", body: str = "") -> ToolResult:
        if not input_enabled():
            return ToolResult(False, "El control directo del teclado está desactivado (NOVA_NO_INPUT=1)")
        mail.clear()
        addresses = [a.strip() for a in re.split(r"[,;]", to) if a.strip()]
        wrong = [a for a in addresses if not is_email(a)]
        if not addresses or wrong:
            return ToolResult(False, f"«{to}» no es una dirección de correo válida. Llama a "
                                     "spell_out con kind=email para que el usuario la dicte "
                                     "letra por letra; no la inventes.")
        if len(subject) + len(body) > MAX_TYPE_CHARS:
            return ToolResult(False, f"El correo pasa de {MAX_TYPE_CHARS} caracteres")
        recipients = ",".join(addresses)
        new = compose_url(recipients, subject, body)
        existing = keyboard.find_tab(is_gmail_tab)
        account = _account_in(existing[1].title) if existing else ""
        try:
            visit = await keyboard.open_site(
                new, is_gmail_tab,
                url_for_tab=lambda title: compose_url(recipients, subject, body, _account_in(title)))
        except RuntimeError as exc:
            return ToolResult(False, str(exc))
        if any(mark in visit.title.lower() for mark in _SIGN_IN):
            return ToolResult(False, "Gmail pide iniciar sesión: el usuario tiene que entrar "
                                     "en su cuenta y volver a pedírmelo.")
        mail.update(to=recipients, subject=subject, body=body, account=account)
        where = "en la pestaña de Gmail que ya tenías" if visit.reused else "en Gmail"
        return ToolResult(True, f"Dejé el borrador para {recipients} {where}, SIN enviar. "
                                "Si el usuario pidió enviarlo, llama a email_send.",
                          {"to": recipients, "reused_tab": visit.reused, "sent": False})

    async def email_send() -> ToolResult:
        """Pulsa Enviar, sólo si en pantalla sigue estando el correo preparado."""
        if not input_enabled():
            return ToolResult(False, "El control directo del teclado está desactivado (NOVA_NO_INPUT=1)")
        if not mail:
            return ToolResult(False, "No hay ningún correo preparado: usa compose_email primero")
        to, sample = mail["to"], (mail["subject"] or mail["body"]).strip()
        if not sample:
            return ToolResult(False, "El correo está vacío: falta el asunto o el texto. No envié nada.")
        # Sólo pestañas de Gmail, y si se conoce la cuenta, la de esa cuenta. El
        # título de la página no sirve para reconocerla: cambia al cargar.
        account = mail["account"]
        same_account = lambda title: is_gmail_tab(title) and account in title
        matches = same_account if account and keyboard.find_tab(same_account) else is_gmail_tab
        if keyboard.find_tab(matches) is None:
            return ToolResult(False, "Ya no veo el borrador en el navegador: no envié nada.")
        try:
            visit = await keyboard.open_site(GMAIL_COMPOSE, matches, url_for_tab=lambda _t: None)
            view = await _read(visit.window)
            shown = _text_in(view.lines, sample)
            if shown is None:
                return ToolResult(False, f"No veo en pantalla el correo para {to}: no envié nada.")
            button = view.send_button()
            if button is not None:
                _click(visit.window, button)
            else:
                _click(visit.window, shown)
                await asyncio.sleep(_CLICK_SETTLE_S)
                if not os_adapter.press_keys(["ctrl", "enter"]):
                    return ToolResult(False, "No pude enviarlo: el correo sigue sin enviar")
            mail.clear()
            await asyncio.sleep(_MAIL_SENT_SETTLE_S)
            view = await _read(visit.window)
        except RuntimeError as exc:
            return ToolResult(False, str(exc))
        if any(_MAIL_SENT.search(l.text) for l in view.lines) or _text_in(view.lines, sample) is None:
            return ToolResult(True, f"Envié el correo a {to}", {"to": to, "sent": True})
        return ToolResult(True, f"Pulsé Enviar en el correo para {to}, pero no pude comprobar "
                                "que saliera: que el usuario lo mire.", {"to": to})

    def _app_window() -> WindowInfo | None:
        return next((w for w in os_adapter.list_windows()
                     if "whatsapp" in (w.process or "").lower()), None)

    async def _bring_app(window: WindowInfo) -> bool:
        if not os_adapter.focus_window(window.handle):
            return False
        await asyncio.sleep(_CLICK_SETTLE_S)
        return True

    async def _open_whatsapp() -> tuple[WindowInfo, str]:
        """La app si está; si no, la pestaña de WhatsApp Web; si no, una nueva."""
        window = _app_window()
        if window is not None:
            if await _bring_app(window):
                return window, "la app de WhatsApp"
            raise RuntimeError("No pude traer WhatsApp al frente")
        app = os_adapter.find_app("WhatsApp")
        if app is not None and "whatsapp" in app.name.lower() and os_adapter.open_app(app):
            deadline = time.monotonic() + _APP_TIMEOUT_S
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
                window = _app_window()
                if window is not None:
                    await _bring_app(window)
                    return window, "la app de WhatsApp"
            log.warning("WhatsApp no abrió ventana; sigo con la versión web")
        visit = await keyboard.open_site(WHATSAPP_WEB, is_whatsapp_tab, url_for_tab=lambda _t: None)
        return visit.window, "WhatsApp Web"

    async def _read(window: WindowInfo) -> _View:
        rect = os_adapter.window_rect(window.handle)
        if rect is None:
            raise RuntimeError("No pude medir la ventana de WhatsApp")
        left, top, right, bottom = rect
        if getattr(os_adapter, "capture_window", None) is not None:
            # La ventana se lee aunque Zoom u otra la tape: con la captura de
            # pantalla, el vídeo flotante de Zoom tapaba la caja de mensaje y
            # el envío se rendía («WhatsApp no llegó a abrir el chat»).
            reading = await ocr.read_window(os_adapter, window.handle, title="WhatsApp")
        else:
            box = {"left": left, "top": top, "width": right - left, "height": bottom - top}
            reading = await ocr.read_screen(box, title="WhatsApp")
        return _View(reading.lines, left, top, right - left, bottom - top)

    def _click(window: WindowInfo, line: ScreenLine, reach: str = "") -> None:
        """Clic en una línea leída, pero sólo donde se ve la ventana de verdad.

        Leer a través de Zoom no basta: el clic va a lo que esté encima. Se prueba
        el centro de la línea y, si está tapado, otros puntos a lo ancho (la caja
        de mensaje ocupa media pantalla). Si todo está tapado, se para."""
        window_at = getattr(os_adapter, "window_at", None)
        x0, y = line.x, line.center[1]
        points = [line.center] + [(x0 + int(line.width * f), y) for f in (0.1, 0.25, 0.75, 0.9)]
        # El texto leído es más corto que la caja: «Escribe un mensaje» ocupa una
        # quinta parte de la caja de mensaje, que sigue hacia la derecha. Con Zoom
        # compartiendo, su barra de controles tapaba justo el texto pero no el
        # resto de la caja (medido: la barra acaba en x=1550, la caja en ~1750).
        rect = os_adapter.window_rect(window.handle) if reach else None
        if rect is not None:
            left, _top, right, _bottom = rect
            width = right - left
            # Sin llegar a los botones de la derecha (micrófono, enviar).
            limit = left + int(width * (0.86 if reach == "composer" else _LEFT_PANEL - 0.02))
            start = x0 + line.width
            if limit > start:
                points += [(start + (limit - start) * k // 6, y) for k in range(1, 7)]
        for point in points:
            if window_at is None or window_at(*point) in (0, window.handle):
                os_adapter.click(*point)
                return
        cover = next((w for w in os_adapter.list_windows() if w.handle == window_at(*line.center)), None)
        name = f"«{cover.title or cover.process}»" if cover is not None else "otra ventana"
        raise RuntimeError(f"{name} está tapando WhatsApp justo donde tengo que hacer clic: muévela "
                           "o minimízala y vuelve a pedírmelo")

    async def _ready(window: WindowInfo) -> _View | None:
        """Espera a que cargue. Una búsqueda a medias tapa la etiqueta «Buscar»:
        un solo Esc, porque el segundo cierra el chat abierto."""
        deadline = time.monotonic() + _READY_TIMEOUT_S
        cleared = False
        while True:
            view = await _read(window)
            if view.search_box() is not None:
                return view
            if time.monotonic() >= deadline:
                return None
            if not cleared:
                os_adapter.press_keys(["esc"])
                cleared = True
            await asyncio.sleep(1.0)

    async def _open_phone_chat(phone: str, text: str) -> tuple[WindowInfo, str, _View]:
        """Abre el chat de un número, aunque no esté guardado, con el texto puesto.

        Por la dirección de WhatsApp y no por el buscador: así no hay que leer
        ni elegir nada en la lista. El texto llega a la caja sin enviarse (los
        saltos de línea van como saltos, no como Enter)."""
        query = urlencode({"phone": phone, "text": text})
        window = _app_window()
        app = None
        if window is None:
            app = os_adapter.find_app("WhatsApp")
            if app is not None and "whatsapp" not in app.name.lower():
                app = None
        if window is not None or app is not None:
            if not os_adapter.open_path(f"whatsapp://send?{query}"):
                raise RuntimeError("No pude abrir WhatsApp")
            deadline = time.monotonic() + _APP_TIMEOUT_S
            while (window := _app_window()) is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError("WhatsApp no llegó a abrirse")
                await asyncio.sleep(0.5)
            await _bring_app(window)
            via = "la app de WhatsApp"
        else:
            url = f"{WHATSAPP_WEB}send?{query}"
            visit = await keyboard.open_site(url, is_whatsapp_tab, url_for_tab=lambda _t: url)
            window, via = visit.window, "WhatsApp Web"
        deadline = time.monotonic() + _PHONE_TIMEOUT_S
        while True:
            await asyncio.sleep(1.0)
            view = await _read(window)
            if any(_INVALID_PHONE.search(l.text) for l in view.lines):
                os_adapter.press_keys(["esc"])
                raise InvalidPhone(f"{display_phone(phone)} no tiene WhatsApp o el número está mal")
            if view.search_box() is not None and view.written(text) is not None:
                return window, via, view
            if time.monotonic() >= deadline:
                raise RuntimeError(f"WhatsApp no llegó a abrir el chat de {display_phone(phone)}")

    async def _draft_by_phone(phone: str, text: str, country_code: str) -> ToolResult:
        number = normalize_phone(phone, country_code or settings["country_code"])
        if number is None:
            return ToolResult(False, f"«{phone}» no sirve como número: pregúntale al usuario el "
                                     "código de su país (p. ej. 51) y pásalo en country_code, o el "
                                     "número completo con +.")
        try:
            window, via, view = await _open_phone_chat(number, text)
        except RuntimeError as exc:
            return ToolResult(False, str(exc))
        name = view.chat_name()
        label = name.text if name is not None else display_phone(number)
        pending.update(chat=label, text=text, phone=number, header_seen=name is not None,
                       search_offset=view.search_box().center[1] - view.top)
        who = f"{label} ({display_phone(number)})" if name is not None else label
        return ToolResult(True, f"Dejé el mensaje escrito para {who} en {via}, SIN enviar. Si el "
                                "usuario pidió enviarlo, llama a whatsapp_send.",
                          {"chat": label, "phone": number, "via": via, "sent": False})

    async def whatsapp_draft(text: str, chat: str = "", phone: str = "",
                             country_code: str = "") -> ToolResult:
        if not input_enabled():
            return ToolResult(False, "El control directo del teclado está desactivado (NOVA_NO_INPUT=1)")
        if not ocr.available():
            return ToolResult(False, "Falta el OCR (RapidOCR): instálalo con npm run deps:core")
        if phone.strip():
            if not text.strip():
                return ToolResult(False, "Falta qué dice el mensaje")
            if len(text) > MAX_TYPE_CHARS:
                return ToolResult(False, f"El mensaje pasa de {MAX_TYPE_CHARS} caracteres")
            pending.clear()
            return await _draft_by_phone(phone, text.strip(), country_code)
        chat, text = _one_line(chat), _one_line(text)
        if not chat or not text:
            return ToolResult(False, "Falta a quién va el mensaje o qué dice")
        if len(text) > MAX_TYPE_CHARS:
            return ToolResult(False, f"El mensaje pasa de {MAX_TYPE_CHARS} caracteres")
        pending.clear()
        try:
            window, via = await _open_whatsapp()
            view = await _ready(window)
            if view is None:
                return ToolResult(False, f"No veo el buscador de chats en {via}. Puede que falte "
                                         "iniciar sesión: el usuario tiene que comprobarlo.")
            box = view.search_box()
            search_y = box.center[1]
            _click(window, box, reach="panel")
            await asyncio.sleep(_CLICK_SETTLE_S)
            if not os_adapter.type_text(chat):
                return ToolResult(False, "No pude escribir el nombre del contacto")
            await asyncio.sleep(_SEARCH_SETTLE_S)

            view = await _read(window)
            # Por debajo de la caja de búsqueda, no en ella: lo que se acaba de
            # escribir se lee en esa misma fila y no es un resultado.
            rows = view.results(chat, below=search_y + max(_SEARCH_ROW_PX, box.height))
            names = list(dict.fromkeys(_normalize(r.text) for r in rows))
            if not rows:
                os_adapter.press_keys(["esc"])
                return ToolResult(False, f"No encontré a «{chat}» en {via}. Llama a spell_out con "
                                         "kind=name para que el usuario dicte el nombre letra por "
                                         "letra y vuelve a intentarlo con lo que confirme.")
            if len(names) > 1:
                os_adapter.press_keys(["esc"])
                options = ", ".join(dict.fromkeys(r.text for r in rows))
                return ToolResult(False, f"Hay varios contactos que encajan con «{chat}»: {options}. "
                                         "Pregúntale al usuario cuál es.")
            _click(window, rows[0], reach="panel")
            await asyncio.sleep(_CHAT_SETTLE_S)

            view = await _read(window)
            header = view.header(chat, search_y)
            if header is None:
                return ToolResult(False, f"Abrí un chat pero no pude comprobar que sea el de "
                                         f"«{chat}», así que no escribí nada.")
            composer = view.composer()
            if composer is None:
                return ToolResult(False, f"La caja de mensaje de «{header.text}» no está vacía o no "
                                         "la veo. No escribí nada para no mezclarlo con lo que hubiera.")
            _click(window, composer, reach="composer")
            await asyncio.sleep(_CLICK_SETTLE_S)
            if not os_adapter.type_text(text):
                return ToolResult(False, "No pude escribir el mensaje")
        except RuntimeError as exc:
            return ToolResult(False, str(exc))

        pending.update(chat=header.text, text=text, search_offset=search_y - view.top)
        preview = text if len(text) <= 60 else text[:57] + "…"
        return ToolResult(True, f"Dejé «{preview}» escrito para {header.text} en {via}, SIN enviar. "
                                "Si el usuario pidió enviarlo, llama a whatsapp_send.",
                          {"chat": header.text, "via": via, "sent": False})

    async def whatsapp_send() -> ToolResult:
        """Enter, sólo si lo que hay en pantalla es lo que se dejó escrito."""
        if not input_enabled():
            return ToolResult(False, "El control directo del teclado está desactivado (NOVA_NO_INPUT=1)")
        if not pending:
            return ToolResult(False, "No hay ningún mensaje preparado: usa whatsapp_draft primero")
        chat, text = pending["chat"], pending["text"]
        try:
            window, via = await _open_whatsapp()
            view = await _read(window)
            if pending.get("header_seen", True) and \
                    view.header(chat, view.top + pending["search_offset"]) is None:
                return ToolResult(False, f"El chat abierto ya no es el de {chat}: no envié nada.")
            if view.composer() is not None:
                return ToolResult(False, f"La caja de mensaje de {chat} está vacía: no envié nada.")
            line = view.written(text)
            if line is None:
                return ToolResult(False, f"No veo el mensaje escrito en el chat de {chat}: no envié nada.")
            _click(window, line, reach="composer")
            await asyncio.sleep(_CLICK_SETTLE_S)
            if not os_adapter.press_keys(["enter"]):
                return ToolResult(False, "No pude pulsar Enter: el mensaje sigue sin enviar")
            pending.clear()
            await asyncio.sleep(_SENT_SETTLE_S)
            view = await _read(window)
        except RuntimeError as exc:
            return ToolResult(False, str(exc))
        if view.composer() is None:
            return ToolResult(True, f"Pulsé Enviar en el chat de {chat}, pero no pude comprobar "
                                    "que saliera: que el usuario lo mire.", {"chat": chat, "via": via})
        return ToolResult(True, f"Envié el mensaje a {chat} por {via}", {"chat": chat, "via": via, "sent": True})

    # ------------------------------------------------------------- en lote
    async def _open_empty_chat(phone: str) -> tuple[WindowInfo, _View]:
        """El chat de un número con la caja vacía, para pegar un archivo.

        Se abre CON un texto de control en la caja y se comprueba que aparece:
        sólo puede estar ahí si WhatsApp abrió la dirección de ese número. Antes
        se abría sin texto y bastaba con ver una caja vacía; una vez la dirección
        no cargó, WhatsApp siguió en el último chat abierto (otra persona) y la
        factura se pegó y se envió ahí. Luego el texto de control se borra.
        """
        marker = f"Nova {phone[-4:]}"
        window, _via, view = await _open_phone_chat(phone, marker)
        line = view.written(marker)
        _click(window, line, reach="composer")
        await asyncio.sleep(_CLICK_SETTLE_S)
        os_adapter.press_keys(["ctrl", "a"])
        await asyncio.sleep(0.1)
        os_adapter.press_keys(["delete"])
        await asyncio.sleep(_CLICK_SETTLE_S)
        view = await _read(window)
        if view.written(marker) is not None or view.composer() is None:
            raise RuntimeError(f"No pude dejar vacía la caja del chat de {display_phone(phone)}")
        return window, view

    def _preview_box(view: _View, filename: str) -> ScreenLine | None:
        """La caja de comentario de la vista previa de un archivo pegado: la de
        «Escribe un mensaje» más arriba de donde está la caja normal (medido en
        WhatsApp Web: 0,82 de alto frente a 0,95), o el nombre del archivo."""
        boxes = [l for l in view.lines if _COMPOSER.search(l.text) and view._x(l) >= _LEFT_PANEL
                 and view._y(l) < 0.9]
        named = any(_normalize(Path(filename).stem)[:14] in _normalize(l.text) for l in view.lines)
        if boxes:
            return boxes[0]
        return view.composer() if named else None

    async def _discard(window: WindowInfo) -> None:
        """Cierra una vista previa sin enviarla: Esc abre «¿Quieres descartar la
        selección?» y hay que pulsar Descartar."""
        os_adapter.press_keys(["esc"])
        await asyncio.sleep(1.0)
        view = await _read(window)
        button = next((l for l in view.lines if _DISCARD.match(l.text.strip())), None)
        if button is not None:
            _click(window, button)
            await asyncio.sleep(0.8)

    async def _send_file(phone: str, caption: str, attachment: str) -> tuple[str, str]:
        """Pega el archivo en el chat, pone el comentario y lo envía."""
        window, view = await _open_empty_chat(phone)
        _click(window, view.composer(), reach="composer")
        await asyncio.sleep(_CLICK_SETTLE_S)
        if not os_adapter.copy_files_to_clipboard([attachment]):
            return "error", "no pude copiar el archivo al portapapeles"
        os_adapter.press_keys(["ctrl", "v"])
        deadline = time.monotonic() + _PREVIEW_TIMEOUT_S
        box = None
        while box is None:
            await asyncio.sleep(1.0)
            box = _preview_box(await _read(window), attachment)
            if box is None and time.monotonic() >= deadline:
                return "error", "WhatsApp no mostró el archivo pegado"
        if caption:
            _click(window, box, reach="composer")
            await asyncio.sleep(_CLICK_SETTLE_S)
            if not os_adapter.type_text(_one_line(caption)):
                await _discard(window)
                return "error", "no pude escribir el comentario"
        if not os_adapter.press_keys(["enter"]):
            await _discard(window)
            return "error", "no pude pulsar Enter"
        await asyncio.sleep(_UPLOAD_SETTLE_S)
        after = await _read(window)
        if _preview_box(after, attachment) is None and after.composer() is not None:
            return "ok", ""
        return "unverified", "no pude comprobar que el archivo saliera"

    async def _send_one(phone: str, text: str, attachment: str = "") -> tuple[str, str]:
        """«ok», «unverified», «invalid» o «error», con el detalle."""
        if attachment:
            try:
                return await _send_file(phone, text, attachment)
            except InvalidPhone as exc:
                return "invalid", str(exc)
            except RuntimeError as exc:
                return "error", str(exc)
        try:
            window, _via, view = await _open_phone_chat(phone, text)
            line = view.written(text)
            _click(window, line, reach="composer")
            await asyncio.sleep(_CLICK_SETTLE_S)
            if not os_adapter.press_keys(["enter"]):
                return "error", "no pude pulsar Enter"
            await asyncio.sleep(_SENT_SETTLE_S)
            after = await _read(window)
        except InvalidPhone as exc:
            return "invalid", str(exc)
        except RuntimeError as exc:
            return "error", str(exc)
        return ("ok", "") if after.composer() is not None else ("unverified", "no pude comprobarlo")

    send = sender or _send_one

    def _load(path: str, phone_column: str, template: str, sheet: str, country_code: str):
        if getattr(searcher, "resolve", None) is not None:
            path = str(searcher.resolve(path))
        if searcher is not None and not searcher.allows(path):
            raise ValueError(f"«{path}» está fuera de las carpetas permitidas")
        table = read_table(path, sheet)
        progress = Progress(folder, path, phone_column, template)
        result = plan(table, phone_column, template, country_code or settings["country_code"],
                      progress.sent)
        return progress, result

    def _gap_for(count: int) -> list:
        """5 s entre mensajes; 10 s si el envío pasa de 20 (lo pidió el usuario)."""
        return settings["gap_large_s"] if count > int(settings["large_batch"]) else settings["gap_s"]

    def _pacing(count: int) -> str:
        size, pause = int(settings["batch_size"]), float(settings["batch_pause_min"])
        gap = sum(_gap_for(count)) / 2
        minutes = schedule_minutes(count, size, pause, gap)
        total = f"{minutes / 60:.1f} h".replace(".", ",") if minutes >= 90 else f"{max(1, round(minutes))} min"
        text = (f" Los mando de {size} en {size}, con {pause:g} minutos de pausa entre cada grupo, así "
                f"que tardaré unos {total}." if count > size else f" Me tomará unos {total}.")
        if count > int(settings["daily_limit"]):
            text += f" Como son más de {settings['daily_limit']}, el resto lo mando al día siguiente."
        return text

    def _batch_question(args: dict) -> str:
        name = Path(args.get("path", "")).name
        try:
            _progress, result = _load(args.get("path", ""), args.get("phone_column", ""),
                                      args.get("template", ""), args.get("sheet", ""),
                                      args.get("country_code", ""))
        except Exception as exc:
            return f"No pude preparar el envío desde «{name}» ({exc}). ¿Lo intento igualmente?"
        if not result.messages:
            return f"No hay ningún mensaje que enviar desde «{name}». ¿Sigo?"
        first = result.messages[0]
        preview = first.text if len(first.text) <= 140 else first.text[:137] + "…"
        extra = ""
        if result.skipped:
            extra += f" {len(result.skipped)} filas se saltan (sin teléfono válido o con datos vacíos)."
        if result.already_sent:
            extra += f" {result.already_sent} ya se enviaron antes y no se repiten."
        return (f"¿Envío {len(result.messages)} WhatsApp desde «{name}»? El primero, a "
                f"{display_phone(first.phone)}: «{preview}».{extra}{_pacing(len(result.messages))}")

    async def _wait(seconds: float) -> bool:
        """Espera; True si se canceló el envío mientras tanto."""
        try:
            await asyncio.wait_for(job["cancel"].wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    async def _until_free() -> bool:
        while not is_idle():
            if await _wait(5):
                return True
        return False

    async def _daily_limit(progress: Progress) -> bool:
        limit = int(settings["daily_limit"])
        recent = progress.sent_last_day()
        if len(recent) < limit:
            return False
        resume = recent[-limit] + DAY_S
        await notify(f"Ya mandé {limit} mensajes en las últimas 24 horas, que es el tope para no "
                     f"arriesgar tu WhatsApp. Sigo a las {time.strftime('%H:%M', time.localtime(resume))}.",
                     speak=True)
        return await _wait(resume - time.time())

    async def _run(messages, progress: Progress, name: str, on_result=None, finish=None) -> None:
        """`on_result(mensaje, enviado, detalle)` tras cada envío; `finish()` da
        el texto con el que se cierra el aviso final (p. ej. lo que faltó)."""
        size = int(settings["batch_size"])
        batches = -(-len(messages) // size)
        streak = 0
        job["state"] = "enviando"
        try:
            for start in range(0, len(messages), size):
                chunk = messages[start:start + size]
                number = start // size + 1
                if start:
                    job["state"] = f"pausa antes de la tanda {number}/{batches}"
                    await notify(f"Van {job['sent']} mensajes enviados. Hago una pausa de "
                                 f"{settings['batch_pause_min']} minutos y sigo con el resto.", speak=True)
                    if await _wait(float(settings["batch_pause_min"]) * 60):
                        return
                if await _until_free():
                    return
                first = "empiezo a mandar" if number == 1 else "sigo mandando"
                # Despierta todo el envío, pausas incluidas: lo pidió el usuario.
                await _activity(True)
                await notify(f"En unos segundos {first} los mensajes por WhatsApp, así que no toques "
                             "el ratón ni el teclado un ratito.", speak=True)
                if await _wait(float(settings["warn_s"])):
                    return
                job["state"] = f"enviando la tanda {number}/{batches}"
                for message in chunk:
                    if await _daily_limit(progress) or await _until_free():
                        return
                    await notify(f"Mandando {job['done'] + 1} de {job['total']}…")
                    status, detail = (await send(message.phone, message.text, message.attachment)
                                      if message.attachment else await send(message.phone, message.text))
                    sent = status in ("ok", "unverified")
                    if on_result is not None:
                        on_result(message, sent, detail)
                    # Uno sin comprobar cuenta como enviado: repetirlo podría duplicarlo.
                    # Pero se dice al final, para que el usuario lo mire en WhatsApp
                    # (pasó: con la vista previa del archivo aún abierta, lo daba por
                    # enviado sin avisar).
                    progress.mark(message.key or message.phone, sent, detail)
                    job["done"] += 1
                    if status == "unverified":
                        job["unverified"].append((message.label, display_phone(message.phone)))
                    if sent:
                        job["sent"] += 1
                        streak = 0
                    else:
                        job["failed"].append((message.label, display_phone(message.phone), detail))
                        # Un número sin WhatsApp es cosa de esa fila; tres errores
                        # seguidos de otra clase, de la pantalla: se para.
                        streak = 0 if status == "invalid" else streak + 1
                        if streak >= 3:
                            job["state"] = "detenido por errores"
                            await notify(f"Tuve que parar el envío porque algo falló tres veces seguidas: "
                                         f"{detail}. Alcancé a mandar {job['sent']}.", speak=True)
                            return
                    low, high = _gap_for(len(messages))
                    if await _wait(random.uniform(float(low), float(high))):
                        return
            job["state"] = "terminado"
            if finish is not None:
                text = await finish()
            else:
                text = f"¡Listo! Terminé de mandar los mensajes, salieron {job['sent']}"
                if job["failed"]:
                    text += f" y {len(job['failed'])} no se pudieron enviar"
                text += "."
            await notify(text + _unverified_note(), speak=True)
        except asyncio.CancelledError:
            await _activity(False)
            raise
        except Exception as exc:
            log.exception("fallo en el envío en lote")
            job["state"] = f"detenido: {exc}"
            await notify(f"Uy, el envío se cortó por un error: {exc}. Alcancé a mandar {job.get('sent', 0)}.",
                         speak=True)
        finally:
            await _activity(False)
            if job.get("cancel") is not None and job["cancel"].is_set():
                job["state"] = "cancelado"

    def _unverified_note() -> str:
        doubtful = job.get("unverified") or []
        if not doubtful:
            return ""
        who = ", ".join(f"{label} ({phone})" for label, phone in doubtful[:5])
        if len(doubtful) > 5:
            who += f" y {len(doubtful) - 5} más"
        return (f" Eso sí, no pude comprobar que saliera el de {who}: míralo en WhatsApp antes de "
                "volver a mandarlo, por si ya llegó." if len(doubtful) == 1 else
                f" Eso sí, no pude comprobar que salieran {len(doubtful)}: {who}. Míralos en WhatsApp "
                "antes de volver a mandarlos, por si ya llegaron.")

    async def whatsapp_batch(path: str, phone_column: str, template: str, sheet: str = "",
                             country_code: str = "") -> ToolResult:
        if not input_enabled():
            return ToolResult(False, "El control directo del teclado está desactivado (NOVA_NO_INPUT=1)")
        if job.get("task") is not None and not job["task"].done():
            return ToolResult(False, f"Ya hay un envío en marcha ({job['state']}). Espera a que "
                                     "termine o cancélalo con whatsapp_batch_cancel.")
        try:
            progress, result = _load(path, phone_column, template, sheet, country_code)
        except (OSError, ValueError) as exc:
            return ToolResult(False, str(exc))
        if not result.messages:
            bad = sum(1 for _row, why in result.skipped if why.startswith("teléfono no válido"))
            hint = (" Los teléfonos no llevan código de país: pregúntale al usuario cuál es "
                    "(p. ej. 51) y pásalo en country_code." if bad and not (country_code or
                                                                          settings["country_code"]) else "")
            done = f" {result.already_sent} ya estaban enviados." if result.already_sent else ""
            return ToolResult(False, f"No hay ningún mensaje que enviar.{done}{hint}")
        name = Path(path).name
        job.clear()
        job.update(cancel=asyncio.Event(), total=len(result.messages), done=0, sent=0, failed=[],
                   unverified=[], state="empezando", name=name, skipped=result.skipped)
        job["task"] = asyncio.create_task(_run(result.messages, progress, name), name="whatsapp-batch")
        skipped = f" {len(result.skipped)} filas se saltaron." if result.skipped else ""
        return ToolResult(True, f"Empecé el envío de {len(result.messages)} WhatsApp desde {name}, "
                                f"por plazos.{_pacing(len(result.messages))}{skipped} Aviso antes de "
                                "cada tanda. Para pararlo: whatsapp_batch_cancel.",
                          {"total": len(result.messages), "skipped": len(result.skipped)})

    async def whatsapp_batch_status() -> ToolResult:
        if not job:
            return ToolResult(True, "No hay ningún envío en lote en esta sesión")
        failed = "; ".join(f"{label} {phone}: {why}" for label, phone, why in job["failed"][:8])
        unverified = job.get("unverified") or []
        doubtful = (f" De los enviados, {len(unverified)} sin poder comprobar que salieran ("
                    + "; ".join(f"{label} {phone}" for label, phone in unverified[:8]) + ")."
                    if unverified else "")
        return ToolResult(True, f"Envío de {job['name']}: {job['state']}. {job['sent']} enviados de "
                                f"{job['total']}, {len(job['failed'])} fallidos"
                                + (f" ({failed})" if failed else "") + "." + doubtful,
                          {k: job[k] for k in ("total", "done", "sent", "state")})

    async def whatsapp_batch_cancel() -> ToolResult:
        if job.get("task") is None or job["task"].done():
            return ToolResult(False, "No hay ningún envío en marcha")
        job["cancel"].set()
        return ToolResult(True, f"Paré el envío de {job['name']}: salieron {job['sent']} de "
                                f"{job['total']}. Si lo vuelves a pedir, sigue donde se quedó.")

    # ----------------------------------------------------------- facturas
    invoices_state: dict = {}

    async def _extract(path: Path, key_field: str = "") -> tuple[list[str], dict[str, str]]:
        texts, scores = await asyncio.to_thread(read_text, path)
        lines = [f"⚠ {t}" if s < 0.95 or inv_garbled(t) else t for t, s in zip(texts, scores)]
        if not texts:
            return lines, {}
        response = await llm_chat([{"role": "system", "content": inv.extraction_prompt(key_field)},
                                   {"role": "user", "content": "\n".join(texts)}])
        return lines, inv.parse_extraction(response.text)

    def _finish_text(items: list, report: Path):
        async def finish() -> str:
            sent = [i for i in items if i.status == "enviado"]
            missing = [i for i in items if i.status != "enviado"]
            try:
                inv.write_report(report, items)
            except Exception as exc:
                log.warning("no pude escribir el reporte: %s", exc)
            if not missing:
                return (f"¡Listo! Ya mandé las {len(sent)} facturas." if len(sent) > 1
                        else "¡Listo! Ya mandé la factura.")
            doubtful = sum(1 for i in missing if i.status == "dudoso")
            text = f"Mandé {len(sent)} de las {len(items)} facturas. Me faltaron {len(missing)}"
            if doubtful:
                text += f", {doubtful} porque no estaba seguro del monto" if doubtful > 1 \
                    else ", una porque no estaba seguro del monto"
            return text + f". Te dejé el detalle en el Excel {report.name}, en la hoja Pendientes."
        return finish

    async def _process(paths: list[Path], table, columns, template: str, progress: Progress,
                       report: Path, key_field: str = "", key_column: str | None = None,
                       attach: bool = False) -> None:
        items: list = []
        invoices_state.update(items=items, report=report, template=template)
        job["state"] = "leyendo facturas"
        for index, path in enumerate(paths, start=1):
            if job["cancel"].is_set():
                return
            item = inv.Invoice(path.name)
            if attach:
                item.extra["attachment"] = str(path)
            items.append(item)
            job["state"] = f"leyendo la factura {index} de {len(paths)}"
            try:
                lines, data = await _extract(path, key_field)
            except Exception as exc:
                log.exception("no pude leer %s", path)
                item.status, item.reason = "falló", f"no pude leerla: {exc}"
                continue
            if not data:
                item.status, item.reason = "falló", "no encontré texto de factura en la imagen"
                continue
            item.amount, item.currency = data.get("monto", ""), data.get("moneda", "")
            item.number = data.get("factura", "")
            if key_field:
                # El usuario dijo qué dato identifica al cliente: se busca ESE dato
                # en su columna. Si el modelo no lo localizó con ese nombre (la
                # factura no dice «código de factura», dice «FACTURA F001-000101»),
                # se prueban los demás datos que sacó, pero siempre contra esa
                # misma columna y exigiendo que se lean sin duda.
                candidates = [data.get(k, "") for k in ("dato_clave", "factura", "documento", "correo")]
                match = inv.Match(None, reason=f"no encontré {key_field} en la factura")
                value, doubtful = "", ""
                for candidate in dict.fromkeys(c for c in candidates if c):
                    ok, why = inv.verify_value(candidate, lines)
                    if not ok:
                        doubtful = doubtful or (why if "seguridad" in why else "")
                        continue
                    found, _col = inv.match_by_key(table, key_column, candidate, key_field)
                    if found.row is not None:
                        match, value = found, candidate
                        break
                    match = found
                if match.row is None and doubtful:
                    item.client = data.get("cliente", "")
                    item.status, item.reason = "dudoso", doubtful
                    continue
                item.extra["key_value"] = value
            else:
                match = inv.match_client(table, columns, data.get("cliente", ""), data.get("correo", ""),
                                         data.get("documento", ""))
            if match.row is None:
                item.client, item.reason = data.get("cliente", ""), match.reason
                continue
            item.client = match.row.get(columns.name or "", "") or data.get("cliente", "")
            phone = normalize_phone(match.row.get(columns.phone or "", ""), settings["country_code"])
            if phone is None:
                item.reason = "el cliente no tiene un teléfono válido en el Excel"
                continue
            item.phone = phone
            key = f"{path.name}|{phone}"
            if key in progress.sent:
                item.status, item.reason = "enviado", "ya se había enviado antes"
                continue
            values = {**match.row, "Nombre": item.client, "Monto": item.amount, "Moneda": item.currency,
                      "Factura": item.number, "Fecha": data.get("fecha", ""),
                      "Correo": match.row.get(columns.email or "", "") or data.get("correo", ""),
                      "Archivo": path.name}
            item.text = inv.render(template, values)
            # El monto se comprueba si va escrito en el mensaje. Si sólo se manda la
            # factura, la cifra la ve el cliente en el propio documento.
            if not attach or inv.uses_field(template, "Monto"):
                ok, why = inv.verify_amount(item.amount, lines)
                if not ok:
                    item.status, item.reason = "dudoso", why
                    continue
            item.status = "listo"
            item.extra["key"] = key

        ready = [i for i in items if i.status == "listo"]
        job["total"] = len(ready)
        if not ready:
            job["state"] = "terminado"
            await notify(await _finish_text(items, report)(), speak=True)
            return
        messages = [Message(i.file, i.phone, i.text, i.extra["key"], i.extra.get("attachment", ""))
                    for i in ready]
        by_key = {i.extra["key"]: i for i in ready}

        def on_result(message, sent, detail):
            item = by_key[message.key]
            item.status, item.reason = ("enviado", detail) if sent else ("falló", detail)

        await _run(messages, progress, report.name, on_result=on_result,
                   finish=_finish_text(items, report))

    def inv_garbled(text: str) -> bool:
        from .document_tools import _garbled

        return _garbled(text)

    async def process_invoices(path: str, clients: str, template: str = "", sheet: str = "",
                               key_field: str = "", key_column: str = "", attach: bool = False) -> ToolResult:
        if not input_enabled():
            return ToolResult(False, "El control directo del teclado está desactivado (NOVA_NO_INPUT=1)")
        if llm_chat is None or read_text is None:
            return ToolResult(False, "Esta función necesita el modelo y el OCR, y no están disponibles")
        if job.get("task") is not None and not job["task"].done():
            return ToolResult(False, f"Ya hay un envío en marcha ({job['state']}). Espera a que "
                                     "termine o cancélalo con whatsapp_batch_cancel.")
        if getattr(searcher, "resolve", None) is not None:
            path, clients = str(searcher.resolve(path)), str(searcher.resolve(clients))
        for target in (path, clients):
            if searcher is not None and not searcher.allows(target):
                return ToolResult(False, f"«{target}» está fuera de las carpetas permitidas")
        try:
            paths = inv.list_images(path)
            table = read_table(clients, sheet)
        except (OSError, ValueError) as exc:
            return ToolResult(False, str(exc))
        if not paths:
            return ToolResult(False, f"No hay facturas (imágenes o PDF) en «{path}»")
        columns = inv.detect_columns(table)
        if columns.phone is None:
            return ToolResult(False, f"No encuentro la columna de teléfonos en {Path(clients).name}. "
                                     f"Columnas: {', '.join(table.columns)}. Pregúntale al usuario cuál es.")
        column_for_key = None
        if key_field:
            from ..files.tables import find_column

            column_for_key = find_column(table, key_column or key_field)
            if key_column and column_for_key is None:
                return ToolResult(False, f"No hay ninguna columna «{key_column}» en el Excel. "
                                         f"Columnas: {', '.join(table.columns)}.")
        elif not (columns.name or columns.email or columns.document):
            return ToolResult(False, "No encuentro en el Excel ninguna columna para reconocer al "
                                     f"cliente (nombre, correo o documento). Columnas: {', '.join(table.columns)}.")
        if not template.strip() and not attach:
            return ToolResult(False, "Falta el mensaje: pregúntale al usuario qué quiere enviar, o si "
                                     "quiere mandar la factura misma (attach=true).")
        unknown = inv.template_problems(template, table)
        if unknown:
            return ToolResult(False, f"El mensaje usa {', '.join('{' + u + '}' for u in unknown)}, que no "
                                     f"existe. Se puede usar: {', '.join('{' + f + '}' for f in inv.INVOICE_FIELDS)} "
                                     f"y las columnas del Excel.")
        folder_path = Path(path).expanduser()
        base = folder_path if folder_path.is_dir() else folder_path.parent
        report = base / f"Nova - facturas {time.strftime('%Y-%m-%d %H%M')}.xlsx"
        progress = Progress(folder, str(folder_path), "facturas", template)
        invoices_state.clear()
        invoices_state["report_source"] = str(folder_path)
        job.clear()
        job.update(cancel=asyncio.Event(), total=len(paths), done=0, sent=0, failed=[], unverified=[],
                   state="empezando", name=folder_path.name, skipped=[])
        job["task"] = asyncio.create_task(_process(paths, table, columns, template, progress, report,
                                                   key_field, column_for_key, attach), name="whatsapp-batch")
        if key_field:
            where = f"la columna «{column_for_key}»" if column_for_key else "la columna en la que aparezca"
            used = f"«{key_field}» de la factura, buscado en {where}; teléfono = «{columns.phone}»"
        else:
            used = ", ".join(f"{k} = «{v}»" for k, v in (("nombre", columns.name), ("correo", columns.email),
                                                          ("documento", columns.document),
                                                          ("teléfono", columns.phone)) if v)
        return ToolResult(True, f"Empecé con {len(paths)} facturas. De la lista de clientes uso: {used}. Envío sin "
                                "preguntar las que tienen el monto y el cliente seguros; las demás las "
                                "aparto. Al terminar aviso qué faltó y dejo un reporte en "
                                f"{report.name}. Puede tardar: los WhatsApp van por plazos.",
                          {"invoices": len(paths), "report": str(report)})

    def _pending_doubtful() -> list:
        return [i for i in invoices_state.get("items", []) if i.status == "dudoso" and i.phone and i.text]

    def _doubtful_question(_args: dict) -> str:
        items = _pending_doubtful()
        if not items:
            return "No quedan facturas con el monto dudoso. ¿Sigo igual?"
        listed = ", ".join(f"{i.client} por {i.currency} {i.amount}".strip() for i in items[:10])
        more = f" y {len(items) - 10} más" if len(items) > 10 else ""
        what = "esa factura" if len(items) == 1 else f"esas {len(items)} facturas"
        return f"Tengo {what} con el monto dudoso: {listed}{more}. ¿Las mando tal como las leí?"

    async def invoices_send_doubtful() -> ToolResult:
        items = _pending_doubtful()
        if not items:
            return ToolResult(False, "No hay facturas con el monto dudoso pendientes")
        if job.get("task") is not None and not job["task"].done():
            return ToolResult(False, "Hay un envío en marcha: espera a que termine")
        report = invoices_state["report"]
        progress = Progress(folder, invoices_state["report_source"], "facturas", invoices_state["template"])
        for item in items:
            item.extra["key"] = f"{item.file}|{item.phone}"
        messages = [Message(i.file, i.phone, i.text, i.extra["key"], i.extra.get("attachment", ""))
                    for i in items]
        by_key = {i.extra["key"]: i for i in items}

        def on_result(message, sent, detail):
            item = by_key[message.key]
            item.status, item.reason = ("enviado", detail) if sent else ("falló", detail)

        job.clear()
        job.update(cancel=asyncio.Event(), total=len(items), done=0, sent=0, failed=[], unverified=[],
                   state="empezando", name=report.name, skipped=[])
        job["task"] = asyncio.create_task(
            _run(messages, progress, report.name, on_result=on_result,
                 finish=_finish_text(invoices_state["items"], report)), name="whatsapp-batch")
        return ToolResult(True, f"Envío las {len(items)} facturas dudosas por plazos. Aviso al terminar.")

    def _mail_question(_args: dict) -> str:
        if not mail:
            return "¿Mando el correo?"
        about = f", el de «{mail['subject']}»" if mail["subject"] else ""
        return f"¿Le mando el correo a {mail['to']}{about}?"

    def _send_question(_args: dict) -> str:
        if not pending:
            return "¿Mando el mensaje de WhatsApp?"
        to = pending["chat"]
        if pending.get("phone") and pending.get("header_seen"):
            to = f"{to}, al {display_phone(pending['phone'])},"
        return f"¿Le mando «{pending['text']}» a {to} por WhatsApp?"

    return [
        Tool(
            name="compose_email",
            description=(
                "Prepara un correo en Gmail: abre la redacción con destinatario, asunto y "
                "texto ya puestos, en la pestaña de Gmail que haya o en una nueva. NO lo "
                "envía: para eso, email_send. Es LA forma de preparar un correo."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string",
                           "description": "Dirección o direcciones separadas por comas"},
                    "subject": {"type": "string", "default": ""},
                    "body": {"type": "string", "default": ""},
                },
                "required": ["to"], "additionalProperties": False,
            },
            # Riesgo bajo: no sale nada de la máquina hasta email_send.
            handler=compose_email, risk=Risk.LOW, timeout_s=30,
        ),
        Tool(
            name="email_send",
            description=(
                "Envía el correo que compose_email acaba de preparar. Pregunta antes al "
                "usuario y comprueba en pantalla que el borrador sigue ahí."
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=email_send, risk=Risk.HIGH, timeout_s=30,
            always_confirm=True, confirm_template=_mail_question,
        ),
        Tool(
            name="whatsapp_draft",
            description=(
                "Abre un chat de WhatsApp y deja el mensaje escrito, sin enviarlo: por nombre "
                "de contacto (`chat`) o por número (`phone`, aunque no esté guardado). Usa la "
                "app si está instalada; si no, la pestaña de WhatsApp Web que haya o una nueva. "
                "Es LA forma de escribir por WhatsApp a una persona."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": MAX_TYPE_CHARS},
                    "chat": {"type": "string", "description": "Contacto o grupo, por nombre"},
                    "phone": {"type": "string", "description": "Número, si no se usa chat"},
                    "country_code": {"type": "string",
                                     "description": "Código de país si el número no lo lleva, p. ej. 51"},
                },
                "required": ["text"], "additionalProperties": False,
            },
            # Riesgo bajo: el mensaje queda en la caja. Enviar es whatsapp_send.
            handler=whatsapp_draft, risk=Risk.LOW, timeout_s=50,
        ),
        Tool(
            name="whatsapp_send",
            description=(
                "Envía el mensaje que whatsapp_draft acaba de dejar escrito. Sólo si el "
                "usuario pidió enviarlo. Comprueba en pantalla el chat y el texto antes."
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=whatsapp_send, risk=Risk.HIGH, timeout_s=25,
            always_confirm=True, confirm_template=_send_question,
        ),
        Tool(
            name="whatsapp_batch",
            description=(
                "Envía un WhatsApp a cada fila de un Excel o CSV, con un mensaje personalizado: "
                "en `template` escribe {Columna} donde vaya el dato de esa fila (p. ej. «Hola "
                "{Nombre}, tu saldo es {Monto}»). Lee el archivo entero por su cuenta. Pregunta "
                "una sola vez, con un resumen, y envía por plazos en segundo plano: tandas con "
                "pausa y tope diario. Usa read_spreadsheet antes para ver las columnas."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "phone_column": {"type": "string", "description": "Columna con los teléfonos"},
                    "template": {"type": "string", "maxLength": MAX_TYPE_CHARS},
                    "sheet": {"type": "string", "default": ""},
                    "country_code": {"type": "string", "default": "",
                                     "description": "Para los números sin código de país, p. ej. 51"},
                },
                "required": ["path", "phone_column", "template"], "additionalProperties": False,
            },
            handler=whatsapp_batch, risk=Risk.HIGH, timeout_s=60,
            always_confirm=True, confirm_template=_batch_question,
        ),
        Tool(
            name="process_invoices",
            description=(
                "Procesa una factura o una carpeta de facturas (imágenes o PDF): lee cada una, saca el "
                "cliente y el monto a pagar, busca al cliente en el Excel de clientes (las "
                "columnas se reconocen solas), y le manda el mensaje por WhatsApp. NO pregunta: "
                "envía directo las que tienen monto y cliente seguros, aparta las dudosas y al "
                "final avisa qué faltó y deja un reporte en Excel. Corre en segundo plano. En "
                "`template` va el mensaje del usuario, con {Nombre}, {Monto}, {Moneda}, "
                "{Factura}, {Fecha}, {Correo} o cualquier columna del Excel."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Carpeta de facturas o una factura"},
                    "clients": {"type": "string", "description": "Lista de clientes: Excel, CSV o PDF"},
                    "key_field": {"type": "string", "default": "",
                                  "description": "Si el usuario dice qué dato de la factura identifica "
                                                 "al cliente (p. ej. «código de cliente», «N° de "
                                                 "suministro», «placa»): ese dato, con sus palabras"},
                    "key_column": {"type": "string", "default": "",
                                   "description": "Columna del Excel donde está ese dato, si la dijo; "
                                                  "vacío = se busca sola"},
                    "template": {"type": "string", "maxLength": MAX_TYPE_CHARS, "default": "",
                                 "description": "El mensaje; con attach=true va como comentario del archivo "
                                                "y puede ir vacío"},
                    "attach": {"type": "boolean", "default": False,
                               "description": "true si el usuario pide enviar la factura misma (el PDF o "
                                              "la foto) en lugar de, o además de, un mensaje"},
                    "sheet": {"type": "string", "default": ""},
                },
                "required": ["path", "clients"], "additionalProperties": False,
            },
            # Medio y no alto: lo que no es seguro no se envía sin preguntar (ver
            # invoices.py). Va en security.auto_approve por defecto, como pidió el usuario.
            handler=process_invoices, risk=Risk.MEDIUM, timeout_s=60,
            confirm_template="¿Proceso las facturas de «{path}» y envío los montos?",
        ),
        Tool(
            name="invoices_send_doubtful",
            description=("Envía también las facturas que quedaron apartadas por monto dudoso, tal "
                         "como se leyeron. Pregunta antes, con la lista de montos."),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=invoices_send_doubtful, risk=Risk.HIGH, timeout_s=10,
            always_confirm=True, confirm_template=_doubtful_question,
        ),
        Tool(
            name="whatsapp_batch_status",
            description="Cómo va el envío de WhatsApp en lote: enviados, fallidos y en qué tanda va.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=whatsapp_batch_status, risk=Risk.LOW, timeout_s=5,
        ),
        Tool(
            name="whatsapp_batch_cancel",
            description="Para el envío de WhatsApp en lote. Lo enviado queda apuntado: repetir la orden sigue donde se quedó.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=whatsapp_batch_cancel, risk=Risk.LOW, timeout_s=5,
        ),
    ]
