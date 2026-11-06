"""Herramientas de aplicaciones y ventanas."""
from __future__ import annotations

import re

from ..os_adapter.base import OsAdapter
from .registry import Risk, Tool, ToolResult


def _words(text: str) -> list[str]:
    table = str.maketrans("áéíóúüñ", "aeiouun")
    return [w for w in re.split(r"[^a-z0-9]+", text.lower().translate(table)) if w]


def running_window(os_adapter: OsAdapter, *names: str):
    """La ventana de una aplicación ya abierta, por el nombre de su PROCESO, no
    por el título: «Spotify» no debe confundirse con una pestaña del navegador
    que se llame «Spotify - Web Player».
    """
    wanted = [w for name in names for w in _words(name) if len(w) >= 4]
    if not wanted:
        return None
    hits = []
    for window in os_adapter.list_windows():
        process = (window.process or "").lower().removesuffix(".exe")
        if len(process) < 4:
            continue
        if any(process == w or process.startswith(w) or process.endswith(w) or w.startswith(process)
               for w in wanted):
            hits.append(window)
    hits.sort(key=lambda w: (not w.foreground, w.minimized))
    return hits[0] if hits else None


def build(os_adapter: OsAdapter, app_allowlist: list[str] | None = None) -> list[Tool]:
    allowed = {a.lower() for a in (app_allowlist or [])}

    async def open_application(application: str, new_window: bool = False) -> ToolResult:
        if allowed and application.lower() not in allowed:
            return ToolResult(False, f"«{application}» no está en la lista de aplicaciones permitidas")
        app = os_adapter.find_app(application)
        # Si ya está abierta (aunque esté minimizada), se trae al frente en vez de
        # lanzarla otra vez. Pasó: «deja Opera abierto» tras minimizarlo todo abrió
        # una ventana nueva de Opera al lado de la del usuario.
        if not new_window:
            window = running_window(os_adapter, application, app.name if app else "")
            if window is not None:
                if os_adapter.focus_window(window.handle):
                    return ToolResult(True, f"{app.name if app else application} ya estaba abierta: la "
                                            f"traje al frente ({window.title})",
                                      {"name": app.name if app else application, "already_open": True})
        if app is None:
            return ToolResult(False, f"No encontré ninguna aplicación instalada parecida a «{application}»")
        if not os_adapter.open_app(app):
            return ToolResult(False, f"No pude abrir {app.name}")
        return ToolResult(True, f"Abrí {app.name}", {"name": app.name})

    async def close_application(application: str) -> ToolResult:
        closed = os_adapter.close_app(application)
        if closed == 0:
            return ToolResult(False, f"No hay ningún proceso abierto que coincida con «{application}»")
        return ToolResult(True, f"Cerré {application}", {"processes": closed})

    async def list_windows() -> ToolResult:
        windows = os_adapter.list_windows()
        listing = [
            {"handle": w.handle, "title": w.title, "process": w.process,
             "minimized": w.minimized, "foreground": w.foreground}
            for w in windows[:30]
        ]
        titles = ", ".join(w["title"][:40] for w in listing[:8]) or "ninguna"
        return ToolResult(True, f"{len(listing)} ventanas abiertas: {titles}", {"windows": listing})

    async def focus_window(title: str) -> ToolResult:
        needle = title.lower()
        for window in os_adapter.list_windows():
            if needle in window.title.lower() or needle in window.process.lower():
                if os_adapter.focus_window(window.handle):
                    return ToolResult(True, f"Puse al frente «{window.title}»")
                return ToolResult(False, f"No pude enfocar «{window.title}»")
        return ToolResult(False, f"No hay ninguna ventana que coincida con «{title}»")

    async def set_window_state(title: str, state: str) -> ToolResult:
        needle = title.lower()
        for window in os_adapter.list_windows():
            if needle in window.title.lower() or needle in window.process.lower():
                ok = os_adapter.set_window_state(window.handle, state)
                return ToolResult(ok, f"{state} sobre «{window.title}»" if ok else "Estado de ventana no válido")
        return ToolResult(False, f"No hay ninguna ventana que coincida con «{title}»")

    async def minimize_all_windows() -> ToolResult:
        os_adapter.minimize_all()
        return ToolResult(True, "Minimicé todas las ventanas")

    return [
        Tool(
            name="open_application",
            description=("Abre una aplicación instalada por su nombre, p. ej. 'spotify', 'visual studio "
                         "code', 'calculadora'. Si ya está abierta, aunque esté minimizada, la trae al "
                         "frente en vez de abrir otra."),
            parameters={
                "type": "object",
                "properties": {"application": {"type": "string", "description": "Nombre de la aplicación"},
                               "new_window": {"type": "boolean", "default": False,
                                              "description": "true sólo si el usuario pide otra ventana nueva"}},
                "required": ["application"], "additionalProperties": False,
            },
            handler=open_application, risk=Risk.LOW, timeout_s=20,
        ),
        Tool(
            name="close_application",
            description="Cierra una aplicación abierta. Intenta un cierre limpio antes de forzarlo.",
            parameters={
                "type": "object",
                "properties": {"application": {"type": "string"}},
                "required": ["application"], "additionalProperties": False,
            },
            handler=close_application, risk=Risk.MEDIUM, timeout_s=20,
            confirm_template="¿Cierro {application}? Puede haber trabajo sin guardar.",
        ),
        Tool(
            name="list_windows",
            description="Lista las ventanas abiertas con su título y proceso. Úsala antes de enfocar o minimizar si no sabes el título exacto.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=list_windows, risk=Risk.LOW, timeout_s=10,
        ),
        Tool(
            name="focus_window",
            description="Trae al frente la ventana cuyo título o proceso coincida con el texto dado.",
            parameters={
                "type": "object",
                "properties": {"title": {"type": "string"}},
                "required": ["title"], "additionalProperties": False,
            },
            handler=focus_window, risk=Risk.LOW, timeout_s=10,
        ),
        Tool(
            name="set_window_state",
            description="Minimiza, maximiza o restaura una ventana.",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "state": {"type": "string", "enum": ["minimize", "maximize", "restore", "show"]},
                },
                "required": ["title", "state"], "additionalProperties": False,
            },
            handler=set_window_state, risk=Risk.LOW, timeout_s=10,
        ),
        Tool(
            name="minimize_all_windows",
            description="Minimiza todas las ventanas y muestra el escritorio.",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=minimize_all_windows, risk=Risk.LOW, timeout_s=10,
        ),
    ]
