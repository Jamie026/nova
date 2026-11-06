"""Máquina de estados del agente.

Las transiciones están declaradas: cualquier salto no previsto lanza excepción en
modo estricto o queda registrado. Esto evita que un bug de audio deje al agente
"escuchando" para siempre.
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Awaitable, Callable

log = logging.getLogger("nova.state")


class AgentState(str, Enum):
    IDLE = "IDLE"
    WAKE_WORD_DETECTED = "WAKE_WORD_DETECTED"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"
    SLEEPING = "SLEEPING"


S = AgentState

TRANSITIONS: dict[AgentState, set[AgentState]] = {
    # IDLE -> SPEAKING: la despedida de «descansa» escrita en el panel de texto.
    S.IDLE: {S.WAKE_WORD_DETECTED, S.LISTENING, S.THINKING, S.SPEAKING, S.ERROR, S.SLEEPING},
    S.WAKE_WORD_DETECTED: {S.LISTENING, S.ERROR, S.IDLE},
    # LISTENING -> SPEAKING: avisos de un trabajo en segundo plano (ver VoiceSession.announce).
    S.LISTENING: {S.THINKING, S.SLEEPING, S.IDLE, S.ERROR, S.LISTENING, S.SPEAKING},
    S.THINKING: {S.EXECUTING, S.SPEAKING, S.LISTENING, S.ERROR, S.IDLE},
    S.EXECUTING: {S.THINKING, S.SPEAKING, S.EXECUTING, S.ERROR, S.IDLE, S.LISTENING},
    # SPEAKING -> EXECUTING: el modelo dijo un preámbulo («Sure, opening it…»)
    # que ya suena mientras ejecuta la herramienta.
    S.SPEAKING: {S.LISTENING, S.IDLE, S.SLEEPING, S.ERROR, S.THINKING, S.EXECUTING},
    S.ERROR: {S.IDLE, S.LISTENING, S.SLEEPING},
    S.SLEEPING: {S.IDLE, S.WAKE_WORD_DETECTED},
}

Listener = Callable[[AgentState, AgentState], Awaitable[None]]


class StateMachine:
    def __init__(self, initial: AgentState = AgentState.IDLE, strict: bool = False):
        self._state = initial
        self._strict = strict
        self._listeners: list[Listener] = []

    @property
    def state(self) -> AgentState:
        return self._state

    def on_change(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def can(self, target: AgentState) -> bool:
        return target in TRANSITIONS.get(self._state, set())

    async def to(self, target: AgentState) -> bool:
        if target is self._state:
            return True
        if not self.can(target):
            msg = f"transición inválida {self._state.value} -> {target.value}"
            if self._strict:
                raise ValueError(msg)
            log.warning(msg)
            return False
        previous, self._state = self._state, target
        log.debug("estado %s -> %s", previous.value, target.value)
        for listener in list(self._listeners):
            await listener(previous, target)
        return True
