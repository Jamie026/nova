"""Orquestador: Intent → Plan → Tool → Observation → Next Action → Result.

El "plan" no es una lista escrita de antemano sino el bucle de razonamiento del
modelo con herramientas: pide una acción, recibe la observación real y decide la
siguiente. Es más robusto que un plan fijo porque el mundo cambia entre pasos —
una página tarda, un resultado no existe, una app no está instalada.

Tres cortafuegos: tope de pasos, timeout por herramienta (en el registro) y
confirmación previa para lo que puede doler.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from ..llm.base import LlmProvider, LlmResponse, ToolCall
from ..tools.registry import ToolRegistry, ToolResult
from .bilingual import split_bilingual, spoken_so_far
from .memory import ActionLog, ActionRecord, ConversationMemory, Preferences
from .prompts import build_system_prompt

log = logging.getLogger("nova.agent")


def _assistant_message(response: LlmResponse) -> dict[str, Any]:
    """Mensaje de asistente en formato OpenAI, aunque el proveedor no lo dé.

    El historial tiene que conservar el par (assistant con tool_calls → tool con
    su resultado). Si el assistant falta o llega vacío, el mensaje `tool` queda
    huérfano, la memoria lo descarta al reconstruir el historial —y con razón,
    porque la API lo rechazaría— y el modelo vuelve a ver la petición original
    como si nada hubiera pasado: pide la misma herramienta otra vez, y otra, hasta
    agotar el tope de pasos. Sintetizarlo aquí convierte ese fallo silencioso en
    algo que no puede ocurrir.
    """
    raw = response.raw_message
    if isinstance(raw, dict) and raw.get("role") == "assistant" and raw.get("tool_calls"):
        return raw
    return {
        "role": "assistant",
        "content": response.text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name,
                             "arguments": json.dumps(call.arguments, ensure_ascii=False)},
            }
            for call in response.tool_calls
        ],
    }

Emit = Callable[[dict[str, Any]], Awaitable[None]]
ConfirmFn = Callable[[str, str, str], Awaitable[bool]]
# El orquestador no conoce la máquina de estados: anuncia en qué fase está y la
# sesión decide qué hacer con ello. Así se puede probar sin montar una FSM.
PhaseFn = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class TurnResult:
    # `reply` es lo que se muestra; `spoken` lo que se dice. Difieren cuando la
    # voz habla un idioma y los subtítulos van en otro; si no, son lo mismo.
    reply: str
    steps: int
    cancelled: bool = False
    spoken: str = ""

    def __post_init__(self) -> None:
        if not self.spoken:
            self.spoken = self.reply


class Orchestrator:
    def __init__(self, llm: LlmProvider, registry: ToolRegistry,
                 conversation: ConversationMemory, actions: ActionLog,
                 preferences: Preferences, emit: Emit, confirm: ConfirmFn,
                 on_phase: PhaseFn | None = None,
                 language: str = "es", max_steps: int = 8,
                 speak_language: str = "", notes=None):
        self._llm = llm
        self._registry = registry
        self._conversation = conversation
        self._actions = actions
        self._preferences = preferences
        self._emit = emit
        self._confirm = confirm
        self._on_phase = on_phase or _ignore_phase
        self._language = language
        self._speak_language = speak_language
        self._notes = notes
        self._max_steps = max_steps
        # Sólo los proveedores que aceptan `on_text` pueden entregar la parte
        # hablada antes de que termine la respuesta.
        try:
            self._streams = "on_text" in inspect.signature(llm.chat).parameters
        except (TypeError, ValueError):
            self._streams = False
        self._cancel = asyncio.Event()

    def cancel(self) -> None:
        self._cancel.set()

    async def warmup(self) -> None:
        """Deja lista la conexión con el modelo antes de la primera frase."""
        warm = getattr(self._llm, "warmup", None)
        if warm is None:
            return
        try:
            await warm()
        except Exception as exc:
            log.debug("precalentamiento del modelo fallido: %s", exc)

    def _system_message(self) -> dict[str, str]:
        return {
            "role": "system",
            "content": build_system_prompt(
                language=self._language,
                speak_language=self._speak_language,
                now=datetime.now().strftime("%A %d/%m/%Y %H:%M"),
                recent_actions=self._actions.render(),
                preferences=self._preferences.render(),
                notes=self._notes.render() if self._notes is not None else "(nada todavía)",
            ),
        }

    async def run(self, user_text: str,
                  early_speak: Callable[[str], None] | None = None) -> TurnResult:
        """`early_speak(texto)` se llama, como mucho una vez, en cuanto la parte
        hablada de la respuesta está completa aunque el subtítulo siga
        llegando. El resultado final lleva ese mismo texto en `spoken`; si no
        coincide (el modelo cambió de idea tras un aviso), quien habla debe
        cortar lo adelantado y decir lo definitivo."""
        self._cancel.clear()
        self._conversation.add_user(user_text)
        schemas = self._registry.schemas()
        steps = 0
        # Firma de cada herramienta ya ejecutada en este turno. Un modelo que
        # repite la misma llamada con los mismos argumentos no está avanzando:
        # o no vio la observación o se atascó. Repetirla otras siete veces sólo
        # gasta tiempo y, si la herramienta tiene efectos, los multiplica.
        executed: dict[str, str] = {}
        nudged = False

        while steps < self._max_steps:
            if self._cancel.is_set():
                return TurnResult("", steps, cancelled=True)
            steps += 1

            messages = [self._system_message(), *self._conversation.messages()]
            early_sent = False

            def on_text(partial: str) -> None:
                nonlocal early_sent
                if early_sent or early_speak is None or not self._bilingual:
                    return
                spoken = spoken_so_far(partial)
                if not spoken:
                    return
                # Lo que se adelanta tiene que ser lo que se va a decir: si esta
                # respuesta va a recibir el aviso de «no has llamado a ninguna
                # herramienta», no se habla todavía.
                if not executed and not nudged and _claims_action(spoken):
                    return
                early_sent = True
                early_speak(spoken)

            started = time.monotonic()
            try:
                if self._streams:
                    response = await self._llm.chat(messages, tools=schemas, on_text=on_text)
                else:
                    response = await self._llm.chat(messages, tools=schemas)
            except Exception as exc:
                log.exception("fallo del modelo")
                return TurnResult(f"No pude contactar con el modelo: {exc}", steps)
            log.debug("modelo: %.2f s (%s)", time.monotonic() - started,
                      "herramientas" if response.wants_tools else "texto")

            if not response.wants_tools:
                raw = response.text.strip() or "Listo."
                # Un «Listo, abrí tu chat» sin haber llamado a nada es una mentira,
                # y pasó: el modelo afirmó haber abierto un chat que no podía abrir.
                # Se le devuelve una sola vez con la observación de que no ejecutó
                # nada; o llama a la herramienta o reconoce que no puede.
                if not executed and not nudged and _claims_action(raw):
                    log.warning("el modelo afirma una acción sin herramienta: %r", raw[:80])
                    self._conversation.add_assistant(raw)
                    self._conversation.add_user(
                        "[sistema] No has llamado a ninguna herramienta en este turno, así "
                        "que eso no ha pasado. Si la acción se puede hacer con una "
                        "herramienta, llámala ahora. Si no se puede, o te falta un dato, "
                        "dilo claramente en vez de afirmar que está hecho.")
                    nudged = True
                    continue
                spoken, subtitle = await self._split_or_translate(raw)
                # Al historial va la respuesta CON las etiquetas, y no es un
                # descuido: la primera versión guardaba sólo el subtítulo por
                # miedo a que el modelo tratara sus «EN:/ES:» como conversación.
                # El efecto fue el contrario. En una charla de cuatro turnos, el
                # modelo veía tres respuestas suyas anteriores en español a secas
                # y a la cuarta imitaba ese patrón: contestaba sin etiquetas y
                # Nova hablaba español con la voz inglesa. Con el historial
                # etiquetado, cada respuesta previa es un ejemplo más del formato.
                self._conversation.add_assistant(self._render_history(spoken, subtitle))
                return TurnResult(subtitle, steps, spoken=spoken)

            self._conversation.add(_assistant_message(response))
            await self._on_phase("EXECUTING")
            repeated: str | None = None
            for call in response.tool_calls:
                if self._cancel.is_set():
                    return TurnResult("", steps, cancelled=True)
                signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}"
                if signature in executed:
                    observation = (
                        f"Esta llamada ya se ejecutó en este turno y dio: {executed[signature]}. "
                        "No la repitas: responde al usuario o usa otra herramienta."
                    )
                    repeated = executed[signature]
                    log.warning("llamada repetida a %s; no se vuelve a ejecutar", call.name)
                else:
                    observation = await self._run_tool(call)
                    executed[signature] = observation
                self._conversation.add({
                    "role": "tool", "tool_call_id": call.id,
                    "name": call.name, "content": observation,
                })
            await self._on_phase("THINKING")

            # Si en un paso entero no hizo nada nuevo, el siguiente tampoco lo
            # hará: se cierra el turno con lo que ya se sabe en lugar de agotar
            # los ocho pasos y responder «me quedé sin pasos».
            if repeated is not None and len(executed) == len(response.tool_calls):
                reply = repeated if repeated.startswith("ERROR") is False else "No pude completar eso."
                self._conversation.add_assistant(reply)
                return TurnResult(reply, steps)  # observación cruda: no lleva etiquetas

        message = "Me quedé sin pasos para esta tarea. ¿La divido en partes más pequeñas?"
        self._conversation.add_assistant(message)
        return TurnResult(message, steps)

    @property
    def _bilingual(self) -> bool:
        return bool(self._speak_language) and self._speak_language != self._language

    def _split(self, raw: str) -> tuple[str, str]:
        if not self._bilingual:
            return raw, raw
        return split_bilingual(raw)

    def _render_history(self, spoken: str, subtitle: str) -> str:
        if not self._bilingual or spoken == subtitle:
            return subtitle
        return "EN: " + spoken + "\nES: " + subtitle

    async def translate_for_speech(self, text: str) -> str:
        """Traduce al idioma de la voz un texto escrito por Nova (no por el modelo).

        Las confirmaciones salen de plantillas en el idioma del usuario. Con la
        voz clonada inglesa, decirlas en español era lo peor de los dos mundos:
        mal pronunciadas y lentas (12 s de reintentos en una sola pregunta de
        cinco palabras). Una llamada corta al modelo tarda ~1 s y devuelve la
        pregunta en el idioma que la voz sabe decir. Si falla, se dice tal cual.
        """
        if not self._bilingual:
            return text
        # Lo que va entre «comillas» es el mensaje literal que se va a enviar, o
        # el nombre de alguien: eso NO se traduce. Pedírselo al modelo no bastó
        # («nos vemos mañana» salía como «see you tomorrow», y el usuario oía un
        # texto distinto del que iba a mandar), así que se sustituye por marcas
        # antes de traducir y se restaura después.
        literals: list[str] = []

        def _hold(match: "re.Match[str]") -> str:
            literals.append(match.group(0))
            return f"[[{len(literals) - 1}]]"

        protected = re.sub(r"«[^»]*»", _hold, text)
        try:
            response = await self._llm.chat([
                {"role": "system", "content": (
                    f"Traduce al {_language_name(self._speak_language)} el texto del usuario. "
                    "Las marcas como [[0]] o [[1]] se copian tal cual, sin tocarlas ni "
                    "moverlas de sitio. Responde sólo con la traducción.")},
                {"role": "user", "content": protected},
            ])
            translated = response.text.strip()
        except Exception as exc:
            log.warning("no pude traducir la confirmación: %s", exc)
            return text
        if not translated:
            return text
        for index, literal in enumerate(literals):
            translated = translated.replace(f"[[{index}]]", literal)
        # Si el modelo se comió alguna marca, mejor la frase original entera que
        # una pregunta a la que le falta el mensaje.
        if any(lit not in translated for lit in literals):
            return text
        return translated

    async def _split_or_translate(self, raw: str) -> tuple[str, str]:
        """Parte la respuesta y, si el modelo se saltó el formato, la traduce.

        Aunque el historial etiquetado hace que el formato se mantenga, «casi
        siempre» no basta cuando el fallo es que Nova hable en el idioma
        equivocado con una voz que suena fatal en él. La red de seguridad es una
        segunda llamada, mínima, que sólo traduce. Cuesta alrededor de un segundo
        y sólo se paga cuando el modelo ha fallado; el resto de las veces no
        existe.
        """
        spoken, subtitle = self._split(raw)
        if not self._bilingual or spoken != subtitle:
            return spoken, subtitle
        log.warning("la respuesta llegó sin las dos versiones; traduzco la voz aparte")
        try:
            response = await self._llm.chat([
                {"role": "system", "content": (
                    f"Traduce al {_language_name(self._speak_language)} el texto del usuario. "
                    "Responde sólo con la traducción, sin comillas ni explicaciones.")},
                {"role": "user", "content": subtitle},
            ])
            traducido = response.text.strip()
            if traducido:
                return traducido, subtitle
        except Exception as exc:
            log.warning("no pude traducir la respuesta: %s", exc)
        return spoken, subtitle

    async def _run_tool(self, call: ToolCall) -> str:
        await self._emit({"type": "tool_call", "name": call.name, "args": call.arguments})

        tool = self._registry.get(call.name)
        if tool is not None and self._registry.needs_confirmation(tool, call.arguments):
            request_id = uuid.uuid4().hex[:8]
            approved = await self._confirm(request_id, tool.confirmation_text(call.arguments), tool.risk.value)
            if not approved:
                result = ToolResult(False, "El usuario no autorizó esta acción")
                await self._emit({"type": "tool_result", "name": call.name, "ok": False, "summary": result.summary})
                return result.as_observation()

        await self._emit({"type": "status", "text": _status_line(call)})
        result = await self._registry.execute(call.name, call.arguments)

        self._actions.add(ActionRecord(call.name, call.arguments, result.ok, result.summary))
        if call.name == "open_application" and result.ok:
            self._preferences.note_app_use(result.data.get("name", ""))

        await self._emit({"type": "tool_result", "name": call.name, "ok": result.ok, "summary": result.summary[:200]})
        return _observation_text(call.name, result)


async def _ignore_phase(_phase: str) -> None:
    return None


# Verbos con los que el modelo afirma haber actuado. Si aparecen al principio de
# una respuesta final y en el turno no se ejecutó ninguna herramienta, la
# afirmación es falsa por construcción.
_ACTION_CLAIM = re.compile(
    r"^\W*(?:EN|ES)?\W*(?:listo|hecho|ya\s+(?:esta|está)|abr[ií]|envi[eé]|cerr[eé]|busqu[eé]|"
    r"escrib[ií]|pegu[eé]|captur[eé]|puse|activ[eé]|done|opened|sent|closed|i\s+(?:opened|"
    r"sent|closed|searched|wrote|typed|pasted|took)|i've\s+(?:opened|sent|closed))\b",
    re.IGNORECASE,
)


def _claims_action(reply: str) -> bool:
    return bool(_ACTION_CLAIM.search(reply.strip()))


def _language_name(code: str) -> str:
    return {"es": "español", "en": "inglés", "pt": "portugués"}.get(code, code)


def _status_line(call: ToolCall) -> str:
    labels = {
        "open_application": "Abriendo {application}",
        "close_application": "Cerrando {application}",
        "browser_open": "Abriendo el navegador",
        "browser_search": "Buscando «{query}»",
        "browser_goto": "Abriendo {url}",
        "browser_new_tab": "Nueva pestaña",
        "browser_close_tab": "Cerrando pestaña",
        "browser_switch_tab": "Cambiando de pestaña",
        "browser_read_page": "Leyendo la página",
        "browser_open_result": "Abriendo el resultado {position}",
        "focus_window": "Poniendo al frente {title}",
        "set_volume": "Volumen al {percent}%",
    }
    template = labels.get(call.name)
    if not template:
        return f"→ {call.name}"
    try:
        return "→ " + template.format(**call.arguments)
    except (KeyError, IndexError):
        return "→ " + template.split("{")[0].strip()


def _observation_text(name: str, result: ToolResult) -> str:
    """Lo que ve el modelo. Para lecturas de página se envía el texto completo."""
    if result.ok and name == "browser_read_page":
        return result.data.get("text", result.summary)[:4000]
    if result.ok and name == "browser_list_tabs":
        tabs = result.data.get("tabs", [])
        if not tabs:
            return "No hay pestañas abiertas"
        listing = "\n".join(f"{t['index']}. {t['title']} — {t['url']}" for t in tabs)
        return f"Pestañas abiertas:\n{listing}"
    return result.as_observation()
