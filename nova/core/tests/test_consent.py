"""Un «sí» dicho en voz alta tiene que ser un sí de verdad, y cualquier duda, no."""
import asyncio

import pytest

from nova.agent.consent import interpret


@pytest.mark.parametrize("texto", ["sí", "Sí.", "si", "dale", "ok", "vale", "hazlo", "claro que sí",
                                   "sí, envíalo", "adelante", "yes", "sure, go ahead", "Confirmo."])
def test_aceptaciones(texto):
    assert interpret(texto) is True


@pytest.mark.parametrize("texto", ["no", "No.", "cancela", "mejor no", "espera", "no lo envíes",
                                   "nope", "cancel", "para, para"])
def test_rechazos(texto):
    assert interpret(texto) is False


@pytest.mark.parametrize("texto", ["", "¿qué?", "mmm", "abre spotify", "Subtítulos por la comunidad",
                                   "sí bueno espera que lo pienso un momento más"])
def test_lo_dudoso_no_es_un_si(texto):
    assert interpret(texto) is not True


def test_si_hay_un_no_gana_el_no():
    # Un mensaje que no se deshace: ante la duda, no se envía.
    assert interpret("sí, bueno, no") is False
    assert interpret("no, sí") is False


# ---------------------------------------------------------------------------
# La sesión: la respuesta por voz resuelve la misma espera que el botón.

from nova.audio.format import FRAME_BYTES
from nova.audio.vad import VadEvent
from nova.config import Config
from nova.session import VoiceSession
from nova.state import StateMachine


class Ipc:
    async def broadcast(self, e): pass
    def broadcast_soon(self, e): pass


class Tts:
    async def speak(self, text, on_level=None, language=None):
        await asyncio.sleep(0.05)
        return True
    async def stop(self): pass


class Stt:
    def __init__(self, texto): self.texto = texto
    async def transcribe(self, pcm, final=True):
        from nova.stt.base import Transcript
        return Transcript(self.texto, final)


class Agente:
    async def translate_for_speech(self, t): return t
    def cancel(self): pass


class VadFalso:
    def __init__(self): self.n = 0; self.speaking = False
    def push(self, frame):
        self.n += 1
        if self.n == 5: return VadEvent.SPEECH_START
        if self.n == 40: return VadEvent.SPEECH_END
        return VadEvent.NONE
    def reset(self): self.n = 0


def _sesion(respuesta):
    s = VoiceSession(Config(Config.load().as_dict()), Ipc(), Stt(respuesta), Tts(), Agente(), StateMachine())
    s._mic = type("M", (), {"drain": lambda self: None})()
    return s


async def _hablar_tras_la_pregunta(sesion):
    # Espera a que la pregunta termine y llegue el turno de escuchar.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if sesion._confirm_listening:
            break
    sesion._vad = VadFalso()
    for _ in range(45):
        await sesion._handle_confirm_audio(bytes(FRAME_BYTES))


@pytest.mark.asyncio
async def test_decir_si_aprueba():
    sesion = _sesion("Sí.")
    tarea = asyncio.create_task(sesion.request_confirmation("c1", "¿Envío «hola»?", "high"))
    await _hablar_tras_la_pregunta(sesion)
    assert await tarea is True


@pytest.mark.asyncio
async def test_decir_no_deniega():
    sesion = _sesion("no, espera")
    tarea = asyncio.create_task(sesion.request_confirmation("c1", "¿Envío «hola»?", "high"))
    await _hablar_tras_la_pregunta(sesion)
    assert await tarea is False


@pytest.mark.asyncio
async def test_algo_que_no_es_si_ni_no_sigue_esperando_y_el_boton_manda():
    sesion = _sesion("abre spotify")
    tarea = asyncio.create_task(sesion.request_confirmation("c1", "¿Envío «hola»?", "high"))
    await _hablar_tras_la_pregunta(sesion)
    await asyncio.sleep(0.05)
    assert not tarea.done()                # no se resolvió con «abre spotify»
    sesion.resolve_confirmation("c1", True)
    assert await tarea is True


@pytest.mark.asyncio
async def test_al_terminar_no_queda_escuchando_confirmaciones():
    sesion = _sesion("sí")
    tarea = asyncio.create_task(sesion.request_confirmation("c1", "¿Envío?", "high"))
    await _hablar_tras_la_pregunta(sesion)
    await tarea
    assert sesion._confirm_id is None and not sesion._confirm_listening
