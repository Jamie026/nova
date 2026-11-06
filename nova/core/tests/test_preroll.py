"""El colchón que evita comerse la primera sílaba.

Este es el bug que hacía que Nova «no entendiera nada»: el VAD necesita
`start_ms` de voz continua para decidir que alguien está hablando, y hasta ese
momento las tramas se tiraban. La grabación empezaba *después* de la primera
sílaba, así que «Abre Spotify» le llegaba a Whisper como «Spotify».

Se prueba con un VAD falso porque lo que importa no es cuándo decide el VAD, sino
qué audio hay guardado cuando lo decide.
"""

import pytest

from nova.audio.format import FRAME_BYTES, FRAME_MS
from nova.audio.vad import VadEvent
from nova.config import Config
from nova.session import VoiceSession
from nova.state import AgentState, StateMachine


class VadFalso:
    """Dice SPEECH_START en la trama que se le indique, y nada antes."""

    def __init__(self, en_trama: int):
        self._en = en_trama
        self._n = 0
        self.speaking = False

    def push(self, _frame: bytes) -> VadEvent:
        self._n += 1
        if self._n == self._en:
            self.speaking = True
            return VadEvent.SPEECH_START
        return VadEvent.NONE

    def reset(self) -> None:
        self._n = 0
        self.speaking = False


class IpcFalso:
    def __init__(self):
        self.eventos = []

    async def broadcast(self, evento):
        self.eventos.append(evento)

    def broadcast_soon(self, evento):
        self.eventos.append(evento)


def _sesion(config: Config) -> VoiceSession:
    machine = StateMachine()
    sesion = VoiceSession(config, IpcFalso(), object(), object(), object(), machine)
    return sesion


def _trama(valor: int) -> bytes:
    """Una trama reconocible: cada una lleva su número repetido."""
    return bytes([valor % 256, 0] * (FRAME_BYTES // 2))


@pytest.mark.asyncio
async def test_conserva_el_audio_anterior_al_inicio_de_voz():
    config = Config(Config.load().as_dict())
    config.set("vad.preroll_ms", 400)
    sesion = _sesion(config)
    await sesion._machine.to(AgentState.WAKE_WORD_DETECTED)
    await sesion._machine.to(AgentState.LISTENING)

    colchon = 400 // FRAME_MS          # 40 tramas
    sesion._vad = VadFalso(en_trama=colchon + 1)

    for i in range(1, colchon + 2):
        await sesion._handle_listening(_trama(i))

    assert sesion._capturing
    # Tiene que haber guardado las 40 anteriores más la que disparó el inicio.
    assert len(sesion._utterance) == (colchon + 1) * FRAME_BYTES
    # Y en orden: la primera guardada es la primera que entró en el colchón.
    assert sesion._utterance[:2] == _trama(1)[:2]
    assert sesion._utterance[-FRAME_BYTES:][:2] == _trama(colchon + 1)[:2]


@pytest.mark.asyncio
async def test_el_colchon_no_crece_sin_limite():
    config = Config(Config.load().as_dict())
    config.set("vad.preroll_ms", 200)
    sesion = _sesion(config)
    await sesion._machine.to(AgentState.WAKE_WORD_DETECTED)
    await sesion._machine.to(AgentState.LISTENING)

    tope = 200 // FRAME_MS
    sesion._vad = VadFalso(en_trama=10_000)   # nunca dispara
    for i in range(1, 500):
        await sesion._handle_listening(_trama(i))

    assert len(sesion._preroll) == tope
    assert not sesion._capturing


@pytest.mark.asyncio
async def test_al_despertar_se_tira_la_palabra_activadora():
    # El colchón contiene el «hey Jarvis» que acaba de sonar. Si no se vaciara,
    # se colaría dentro de la orden y Whisper lo transcribiría como parte de ella.
    config = Config(Config.load().as_dict())
    sesion = _sesion(config)
    await sesion._machine.to(AgentState.WAKE_WORD_DETECTED)
    await sesion._machine.to(AgentState.LISTENING)
    sesion._vad = VadFalso(en_trama=10_000)
    for i in range(1, 30):
        await sesion._handle_listening(_trama(i))
    assert sesion._preroll

    sesion._mic = type("M", (), {"drain": lambda s: None})()
    await sesion._begin_listening(greet=False)
    assert not sesion._preroll


# ---------------------------------------------------------------------------
# Interrumpir a Nova: el eco de los altavoces no cuenta, la voz del usuario sí.

def _trama_con_nivel(rms: float) -> bytes:
    import numpy as np
    n = FRAME_BYTES // 2
    onda = np.sin(np.linspace(0, 2 * np.pi * 8, n)) * rms * np.sqrt(2)
    return (onda * 32767).astype(np.int16).tobytes()


@pytest.mark.asyncio
async def test_el_eco_flojo_no_interrumpe_a_nova():
    config = Config(Config.load().as_dict())
    sesion = _sesion(config)
    for estado in (AgentState.WAKE_WORD_DETECTED, AgentState.LISTENING,
                   AgentState.THINKING, AgentState.SPEAKING):
        await sesion._machine.to(estado)

    class VadQueSiempreOyeVoz:
        """Como webrtcvad con la voz de Nova por los altavoces: todo es habla."""
        def __init__(s): s._n = 0; s.speaking = False
        def push(s, frame):
            if frame == bytes(FRAME_BYTES):      # silencio inyectado por el filtro
                s._n = 0
                return VadEvent.NONE
            s._n += 1
            return VadEvent.SPEECH_START if s._n == 40 else VadEvent.NONE
        def reset(s): s._n = 0
    sesion._barge_vad = VadQueSiempreOyeVoz()

    cortes = []
    class TtsEspia:
        async def stop(s): cortes.append("stop")
    sesion._tts = TtsEspia()

    # Eco: 2 segundos de "voz" a un nivel de 0,015, por debajo del umbral de 0,04.
    for _ in range(200):
        await sesion._handle_barge_in(_trama_con_nivel(0.015))
    assert cortes == []
    assert sesion._machine.state is AgentState.SPEAKING


@pytest.mark.asyncio
async def test_la_voz_fuerte_si_interrumpe_y_conserva_el_principio():
    config = Config(Config.load().as_dict())
    sesion = _sesion(config)
    for estado in (AgentState.WAKE_WORD_DETECTED, AgentState.LISTENING,
                   AgentState.THINKING, AgentState.SPEAKING):
        await sesion._machine.to(estado)
    sesion._barge_vad = VadFalso(en_trama=40)
    sesion._mic = type("M", (), {"drain": lambda s: None})()

    class TtsEspia:
        def __init__(s): s.parado = False
        async def stop(s): s.parado = True
    sesion._tts = TtsEspia()
    sesion._agent = type("A", (), {"cancel": lambda s: None})()

    for _ in range(40):
        await sesion._handle_barge_in(_trama_con_nivel(0.12))   # la voz del usuario
    assert sesion._tts.parado
    assert sesion._machine.state is AgentState.LISTENING
    assert sesion._capturing
    # Las 40 tramas que hicieron falta para decidir están en la grabación.
    assert len(sesion._utterance) == 40 * FRAME_BYTES


def test_varias_palabras_activadoras_y_cambio_en_caliente():
    from nova.audio.wakeword import WhisperKeywordSpotter

    spotter = WhisperKeywordSpotter.__new__(WhisperKeywordSpotter)
    spotter._threshold = 0.72
    spotter.set_keywords("corrin, nova")
    assert spotter._matches("¡Corrin!") and spotter._matches("Colin") and spotter._matches("No va.")
    assert not spotter._matches("hola qué tal")
    spotter.set_keywords("corrin")
    assert not spotter._matches("No va.")          # ya sólo corrin
    assert spotter._matches("Córin.")
    assert "Corrin" in spotter._prompt
