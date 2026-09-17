"""Composición de la aplicación: aquí se decide qué implementación usa cada interfaz.

Es el único sitio donde aparecen los nombres concretos de proveedores. El resto
del código depende de interfaces.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .agent.memory import ActionLog, ConversationMemory, Preferences
from .agent.orchestrator import Orchestrator
from .browser.controller import BrowserController
from .capture.screen import ScreenCapture
from .config import Config
from .files.search import FileSearcher
from .ipc.server import IpcServer
from .os_adapter import build_os_adapter
from .paths import screenshots_dir
from .state import StateMachine
from .tools import applications as app_tools
from .tools import browser_tools, control_tools, file_tools, memory_tools, screen_tools, system_tools
from .agent.notes import UserNotes
from .paths import memory_file
from .tools.registry import ToolRegistry
from .session import VoiceSession

log = logging.getLogger("nova.app")


def build_file_searcher(config: Config, os_adapter) -> FileSearcher:
    """Raíces de búsqueda: las declaradas a mano o, si no hay, las del usuario.

    Que el valor por defecto sea «lo que el SO diga» y no una lista escrita en el
    código evita el error clásico de buscar en C:\\Users\\<nombre>\\Documents
    cuando esa carpeta está redirigida a OneDrive.
    """
    roots = [str(r) for r in (config.get("files.search_roots") or []) if r]
    if not roots:
        roots = list(os_adapter.user_folders().values())
        log.info("alcance de archivos: %s", ", ".join(roots) or "ninguna carpeta detectada")
    return FileSearcher(
        roots=roots,
        max_results=int(config.get("files.max_results", 12)),
        max_depth=int(config.get("files.max_depth", 6)),
        budget_s=float(config.get("files.budget_s", 6.0)),
    )


def build_llm(config: Config):
    if config.mock or config.get("llm.provider") == "mock":
        from .llm.mock_llm import MockLlm

        log.warning("LLM en modo simulado")
        return MockLlm()
    from .llm.deepseek import DeepSeekProvider

    return DeepSeekProvider(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        model=config.get("llm.model", "deepseek-chat"),
        base_url=config.get("llm.base_url", "https://api.deepseek.com"),
        temperature=float(config.get("llm.temperature", 0.3)),
        timeout=float(config.get("llm.request_timeout_s", 60)),
        max_tokens=int(config.get("llm.max_tokens", 300) or 0) or None,
    )


def build_stt(config: Config):
    if config.mock or config.get("stt.provider") == "mock":
        from .stt.mock_stt import MockStt

        # Este aviso importa: en modo simulado la transcripción NO viene del
        # micrófono, son frases de ejemplo en rotación. Sin decirlo, parece que
        # Nova te oye mal cuando en realidad no te está oyendo en absoluto.
        log.warning("STT en modo simulado: las transcripciones son frases de ejemplo, "
                    "no lo que digas por el micrófono")
        return MockStt()
    from .stt.faster_whisper_stt import FasterWhisperStt

    return FasterWhisperStt(
        model=config.get("stt.model", "small"),
        cpu_model=config.get("stt.cpu_model", "tiny"),
        device=config.get("stt.device", "auto"),
        compute_type=config.get("stt.compute_type", "auto"),
        language=config.get("language", "es"),
        beam_size=int(config.get("stt.beam_size", 5)),
        cpu_threads=int(config.get("stt.cpu_threads", 0)),
        vad_filter=bool(config.get("stt.vad_filter", True)),
        initial_prompt=config.get("stt.initial_prompt", ""),
    )


def build_tts(config: Config):
    if config.mock or config.get("tts.provider") == "mock":
        from .tts.mock_tts import MockTts

        return MockTts()

    if config.get("tts.provider") in ("chatterbox", "xtts"):
        # "xtts" se acepta por compatibilidad con .env antiguos: la voz clonada
        # ahora es Chatterbox Turbo (ver tts/chatterbox_engine.py).
        engine = _build_chatterbox(config)
        if engine is not None:
            return engine
        # Sin referencia no hay voz que clonar, pero quedarse mudo es peor que
        # hablar con otra voz: se avisa y se sigue con edge-tts.

    from .tts.edge_tts_engine import EdgeTts

    return EdgeTts(
        voice=config.get("tts.voice", "es-PE-CamilaNeural"),
        rate=config.get("tts.rate", "+8%"),
        volume=config.get("tts.volume", "+0%"),
    )


def speak_language(config: Config) -> str:
    """Idioma de la voz. Por defecto, el mismo que el de la interfaz."""
    return config.get("tts.speak_language") or config.get("language", "es")


def _build_chatterbox(config: Config):
    from .paths import app_data_dir

    reference = Path(config.get("tts.reference") or app_data_dir() / "voices" / "reference.wav")
    if not reference.is_file():
        log.error(
            "voz clonada pedida pero no hay referencia en %s; sigo con edge-tts. "
            "Créala con: npm run voice:build \"<carpeta con audios>\"", reference,
        )
        return None
    try:
        from .tts.chatterbox_engine import ChatterboxEngine
    except ImportError as exc:
        log.error("falta la dependencia de la voz clonada (%s); sigo con edge-tts. "
                  "Instálala con: npm run deps:voice", exc)
        return None

    return ChatterboxEngine(
        reference=reference,
        language=speak_language(config),
        device=config.get("tts.device", "auto"),
        output_device=config.get("audio.output_device") or None,
    )
class NovaApp:
    def __init__(self, config: Config):
        self.config = config
        self.ipc = IpcServer(config.get("ipc.host", "127.0.0.1"), int(config.get("ipc.port", 8765)))
        self.machine = StateMachine()
        self.os_adapter = build_os_adapter()
        self.browser = BrowserController(
            channel=config.get("browser.channel", "chrome"),
            cdp_port=int(config.get("browser.cdp_port", 9222)),
            attach_timeout_s=int(config.get("browser.attach_timeout_s", 20)),
            search_template=config.get("browser.default_search"),
        )

        self.searcher = build_file_searcher(config, self.os_adapter)
        self.capture = ScreenCapture(
            Path(config.get("screen.output_dir") or screenshots_dir())
        )

        self.registry = ToolRegistry(
            allowlist=config.get("security.tool_allowlist", []),
            risk_policy={"low": config.get("security.low", "auto"),
                         "medium": config.get("security.medium", "confirm"),
                         "high": config.get("security.high", "confirm")},
            auto_approve=config.get("security.auto_approve", []),
        )
        self.registry.register_all(app_tools.build(self.os_adapter, config.get("security.app_allowlist", [])))
        self.registry.register_all(browser_tools.build(
            self.browser, self.os_adapter,
            mode=config.get("browser.mode", "auto"),
            prefer=config.get("browser.prefer", "")))
        self.registry.register_all(system_tools.build(self.os_adapter))
        self.registry.register_all(file_tools.build(self.os_adapter, self.searcher))
        self.registry.register_all(screen_tools.build(self.os_adapter, self.capture))
        if config.get("control.enabled", True):
            self.registry.register_all(control_tools.build(self.os_adapter, self.searcher))
        else:
            # No registrarlas es mejor que rechazarlas: el modelo no las ve en el
            # esquema, así que no las propone y no gasta un turno en un «no puedo».
            log.warning("control directo del escritorio desactivado")

        self.notes = UserNotes(memory_file())
        self.registry.register_all(memory_tools.build(self.notes))
        self.conversation = ConversationMemory(int(config.get("memory.max_turns", 24)))
        self.actions = ActionLog(int(config.get("memory.max_actions", 20)))
        self.preferences = Preferences()

        self.llm = build_llm(config)
        self.stt = build_stt(config)
        self.tts = build_tts(config)

        self.orchestrator = Orchestrator(
            llm=self.llm, registry=self.registry, conversation=self.conversation,
            actions=self.actions, preferences=self.preferences,
            emit=self.ipc.broadcast, confirm=self._confirm, on_phase=self._set_phase,
            language=config.get("language", "es"),
            speak_language=speak_language(config),
            max_steps=int(config.get("llm.max_steps", 8)),
            notes=self.notes,
        )
        self.session = VoiceSession(config, self.ipc, self.stt, self.tts, self.orchestrator, self.machine)
        self._register_commands()

    async def _confirm(self, request_id: str, text: str, risk: str) -> bool:
        return await self.session.request_confirmation(request_id, text, risk)

    async def _set_phase(self, phase: str) -> None:
        from .state import AgentState

        await self.machine.to(AgentState(phase))

    def _register_commands(self) -> None:
        async def on_text_input(message: dict[str, Any]) -> None:
            text = (message.get("text") or "").strip()
            if text:
                await self.session.handle_text(text)

        async def on_confirm_response(message: dict[str, Any]) -> None:
            self.session.resolve_confirmation(message.get("id", ""), bool(message.get("approved")))

        async def on_stop(_message: dict[str, Any]) -> None:
            await self.session.interrupt()

        async def on_wake(_message: dict[str, Any]) -> None:
            await self.session.force_wake()

        async def on_get_settings(_message: dict[str, Any]) -> None:
            from .audio.mic import list_devices

            await self.ipc.broadcast({
                "type": "settings",
                "data": {"config": self.config.as_dict(),
                         "devices": list_devices(),
                         "tools": [t.name for t in self.registry.enabled()]},
            })

        async def on_set_settings(message: dict[str, Any]) -> None:
            self.config.patch(message.get("patch", {}))
            self.config.save()
            applied = self.session.apply_live_settings()
            await self.ipc.broadcast({"type": "settings", "data": {"config": self.config.as_dict()}})
            nota = "Ajustes guardados."
            if applied:
                nota += " Aplicado ya: " + ", ".join(applied) + "."
            nota += " Voz y modelo necesitan reiniciar."
            await self.ipc.broadcast({"type": "status", "text": nota})

        self.ipc.on("text_input", on_text_input)
        self.ipc.on("confirm_response", on_confirm_response)
        self.ipc.on("stop", on_stop)
        self.ipc.on("wake", on_wake)
        self.ipc.on("get_settings", on_get_settings)
        self.ipc.on("set_settings", on_set_settings)

    async def start(self) -> None:
        await self.ipc.start()
        # El inventario de aplicaciones sale de Get-StartApps por PowerShell y
        # tarda ~10 s. Se calculaba la primera vez que alguien pedía abrir algo,
        # o sea que la primera orden de la sesión («abre Discord») esperaba diez
        # segundos mirando al avatar. Aquí se lanza al arrancar, en un hilo, y
        # cuando llega la orden ya está.
        if not self.config.mock:
            asyncio.get_running_loop().run_in_executor(
                None, self.os_adapter.list_installed_apps)
        if self.config.mock:
            await self.ipc.broadcast({
                "type": "status",
                "text": "Modo simulado: las frases son de ejemplo, no se escucha el micrófono.",
            })
        await self.session.start()

    async def shutdown(self) -> None:
        log.info("cerrando Nova")
        await self.session.stop()
        await self.browser.close()
        await self.llm.close()
        await self.ipc.stop()
