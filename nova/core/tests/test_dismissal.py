"""«Descansa» y «para» se obedecen al instante, sin pasar por el modelo."""
import asyncio

import pytest

from nova.agent.dismissal import SLEEP, STOP, interpret


@pytest.mark.parametrize("texto", ["descansa", "Buen trabajo, Corrin. Descansa.", "Corrin, ya puedes descansar.",
                                   "Eso es todo, gracias.", "hasta luego", "Nada más.", "¿Puedes descansar?",
                                   "adiós", "vete a dormir"])
def test_despedidas(texto):
    assert interpret(texto) == SLEEP


@pytest.mark.parametrize("texto", ["Para.", "Corrin, para ya.", "para, para", "detente", "cancela eso",
                                   "No, no, no.", "Stop", "cállate"])
def test_ordenes_de_parar(texto):
    assert interpret(texto) == STOP


@pytest.mark.parametrize("texto", ["", "para abrir youtube", "nada más abre Spotify", "no abras nada",
                                   "abre youtube y busca un anime", "qué hora es", "gracias",
                                   "descansa un poco y luego abre discord ok bueno vale ya",
                                   "Subtítulos por la comunidad de Amara.org"])
def test_lo_demas_va_al_modelo(texto):
    assert interpret(texto) is None


# ---------------------------------------------------------------------------
# La sesión: se calla, se despide y se duerme; o para y vuelve a escuchar.

from nova.audio.format import FRAME_BYTES
from nova.audio.vad import VadEvent
from nova.config import Config
from nova.session import VoiceSession
from nova.state import AgentState, StateMachine
from nova.stt.base import Transcript


class Ipc:
    def __init__(self): self.events = []
    async def broadcast(self, e): self.events.append(e)
    def broadcast_soon(self, e): pass


class Tts:
    def __init__(self): self.said = []; self.stopped = 0
    async def speak(self, text, on_level=None, language=None):
        self.said.append(text)
        await asyncio.sleep(0.02)
        return True
    async def stop(self): self.stopped += 1


class Stt:
    def __init__(self, texto): self.texto = texto; self.calls = 0
    async def transcribe(self, pcm, final=True, strict=True):
        self.calls += 1
        return Transcript(self.texto, final)


class Agente:
    def __init__(self): self.runs = 0; self.cancelled = 0
    async def run(self, text, early_speak=None):
        self.runs += 1
        await asyncio.sleep(10)
    async def translate_for_speech(self, t): return t
    def cancel(self): self.cancelled += 1


class VadFalso:
    def __init__(self): self.n = 0
    def push(self, frame):
        self.n += 1
        if self.n == 5: return VadEvent.SPEECH_START
        if self.n == 40: return VadEvent.SPEECH_END
        return VadEvent.NONE
    def reset(self): self.n = 0


def _sesion(oido):
    machine = StateMachine()
    s = VoiceSession(Config(Config.load().as_dict()), Ipc(), Stt(oido), Tts(), Agente(), machine)
    s._mic = type("M", (), {"drain": lambda self: None})()
    return s


@pytest.mark.asyncio
async def test_descansa_no_pasa_por_el_modelo_y_se_duerme():
    s = _sesion("Descansa.")
    await s.handle_text("Buen trabajo, Corrin. Descansa.")
    assert s._agent.runs == 0
    assert s._tts.said == ["Okay, resting."]
    assert s._machine.state is AgentState.IDLE
    subtitulos = [e for e in s._ipc.events if e["type"] == "assistant_text"]
    assert subtitulos and subtitulos[-1]["text"] == "Descansando."


@pytest.mark.asyncio
async def test_para_corta_y_vuelve_a_escuchar():
    s = _sesion("para")
    await s.handle_text("abre youtube")          # un turno largo en marcha
    await asyncio.sleep(0.02)
    assert s._agent.runs == 1 and s._turn_task is not None
    await s.handle_text("Corrin, para.")
    assert s._agent.cancelled >= 1
    assert s._turn_task is None
    assert s._tts.said == []                     # parar no se comenta en voz
    assert s._machine.state is AgentState.LISTENING


@pytest.mark.asyncio
async def test_descansa_mientras_ejecuta_corta_la_tarea():
    """Antes el micrófono se ignoraba mientras Nova pensaba o ejecutaba."""
    s = _sesion("Descansa.")
    await s.handle_text("abre youtube y busca un anime")
    await asyncio.sleep(0.02)
    await s._machine.to(AgentState.EXECUTING)
    s._barge_vad = VadFalso()
    s._barge_min_level = 0.0                     # tramas de silencio cuentan como voz
    for _ in range(45):
        await s._handle_working(bytes(FRAME_BYTES))
    assert s._side_task is not None
    await s._side_task
    assert s._stt.calls == 1
    assert s._turn_task is None                  # la tarea se canceló
    assert s._tts.said == ["Okay, resting."]
    assert s._machine.state is AgentState.IDLE


@pytest.mark.asyncio
async def test_hablar_de_otra_cosa_mientras_ejecuta_no_la_corta():
    s = _sesion("sí, mañana vamos al cine")
    await s.handle_text("abre youtube y busca un anime")
    await asyncio.sleep(0.02)
    await s._machine.to(AgentState.EXECUTING)
    s._barge_vad = VadFalso()
    s._barge_min_level = 0.0
    for _ in range(45):
        await s._handle_working(bytes(FRAME_BYTES))
    await s._side_task
    assert s._turn_task is not None and not s._turn_task.done()
    assert s._machine.state is AgentState.EXECUTING
    await s._cancel_turn()
