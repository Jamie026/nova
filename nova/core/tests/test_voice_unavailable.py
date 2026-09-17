"""Si la voz no carga (Windows sin memoria, error 1455), Nova sigue por escrito.

Pasó: Chatterbox tardó 88 s en cargar con un juego abierto y acabó en «El
archivo de paginación es demasiado pequeño» (WinError 1455). El turno terminó
en ERROR, el avatar estuvo «hablando» minuto y medio sin sonido, y la voz se
quedó muerta hasta reiniciar.
"""
import asyncio

import pytest

from nova import session as session_module
from nova.config import Config
from nova.session import VoiceSession
from nova.state import AgentState, StateMachine


class Ipc:
    def __init__(self): self.events = []
    async def broadcast(self, e): self.events.append(e)
    def broadcast_soon(self, e): pass


class VozQueNoCarga:
    """Como el motor real: `ready` es False hasta que warmup() termina."""
    def __init__(self, fallos):
        self.fallos = fallos; self.intentos = 0; self.ready = False; self.said = []
    async def warmup(self):
        self.intentos += 1
        if self.intentos <= self.fallos:
            raise OSError(1455, "El archivo de paginación es demasiado pequeño")
        self.ready = True
    async def speak(self, text, on_level=None, language=None):
        if not self.ready:
            await self.warmup()
        self.said.append(text)
        return True
    async def stop(self): pass
    async def close(self): pass


class Turno:
    def __init__(self, reply, spoken): self.reply = reply; self.spoken = spoken; self.cancelled = False


class Agente:
    async def run(self, text, early_speak=None):
        return Turno("Hola, muy bien, gracias.", "Hey, I'm doing great, thanks.")
    async def translate_for_speech(self, t): return t
    def cancel(self): pass


def _sesion(tts):
    s = VoiceSession(Config(Config.load().as_dict()), Ipc(), None, tts, Agente(), StateMachine())
    s._mic = type("M", (), {"drain": lambda self: None})()
    return s


@pytest.mark.asyncio
async def test_sin_voz_la_respuesta_queda_escrita_y_no_hay_error(monkeypatch):
    tts = VozQueNoCarga(fallos=99)
    s = _sesion(tts)
    estados = []
    s._machine.on_change(lambda a, b: _anota(estados, b))
    esperas = []

    async def dormir(seg): esperas.append(seg)
    monkeypatch.setattr(session_module.asyncio, "sleep", dormir)

    await s._run_turn("hola, cómo estás")
    assert AgentState.ERROR not in estados
    assert s._machine.state is AgentState.LISTENING
    assert tts.said == []
    subtitulos = [e for e in s._ipc.events if e["type"] == "assistant_text"]
    assert subtitulos[-1]["text"] == "Hola, muy bien, gracias."
    avisos = [e["text"] for e in s._ipc.events if e["type"] == "status"]
    assert "Preparando la voz..." in avisos and any("Sin voz" in a for a in avisos)
    # Se dio tiempo de leer la respuesta antes de volver a escuchar.
    assert esperas and 1.5 < esperas[0] <= 10


async def _anota(lista, estado):
    lista.append(estado)


@pytest.mark.asyncio
async def test_la_precarga_reintenta_con_esperas_crecientes(monkeypatch):
    tts = VozQueNoCarga(fallos=2)
    s = _sesion(tts)
    esperas = []

    async def dormir(seg): esperas.append(seg)
    monkeypatch.setattr(session_module.asyncio, "sleep", dormir)

    await s._warmup_tts()
    assert tts.ready and tts.intentos == 3
    assert esperas == [30, 60]


@pytest.mark.asyncio
async def test_la_precarga_se_rinde_tras_los_reintentos(monkeypatch):
    tts = VozQueNoCarga(fallos=99)
    s = _sesion(tts)

    async def dormir(seg): pass
    monkeypatch.setattr(session_module.asyncio, "sleep", dormir)

    await s._warmup_tts()
    assert tts.intentos == 1 + len(session_module._TTS_RETRY_DELAYS_S)
