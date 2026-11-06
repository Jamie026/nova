"""La palabra activadora es «Corrin» y Nova no atiende a ninguna otra."""
import asyncio

import numpy as np

from nova.audio import wakeword
from nova.audio.format import FRAME_BYTES, SAMPLE_RATE
from nova.audio.wakeword import NullWakeWord, WhisperKeywordSpotter, build_wake_word
from nova.config import DEFAULTS, Config
from nova.session import VoiceSession


def _config(**wake):
    config = Config(Config.load().as_dict())
    config.patch({"wake": wake})
    return config


def _spotter(window_ms: int = 1200, heard: str = "hola qué tal"):
    class _Segment:
        text = heard

    class _Model:
        def __init__(self):
            self.windows = []

        def transcribe(self, audio, **_):
            self.windows.append(audio)
            return [_Segment()], None

    spotter = WhisperKeywordSpotter.__new__(WhisperKeywordSpotter)
    spotter._threshold = 0.85
    spotter.set_keywords("corrin")
    spotter._window_bytes = SAMPLE_RATE * 2 * window_ms // 1000
    spotter._buffer = bytearray()
    spotter._energy_gate = 0.0
    spotter._model = _Model()
    return spotter


def _voice(frames: int) -> bytes:
    return (np.full(frames * FRAME_BYTES // 2, 8000, dtype=np.int16)).tobytes()


def test_por_defecto_la_palabra_es_corrin_con_el_motor_que_la_reconoce():
    assert DEFAULTS["wake"]["keyword"] == "corrin"
    assert DEFAULTS["wake"]["engine"] == "whisper"


def test_sin_whisper_no_se_cae_a_hey_jarvis(monkeypatch):
    def rota(_config):
        raise RuntimeError("sin modelo")

    tocado = []
    monkeypatch.setitem(wakeword._BUILDERS, "whisper", rota)
    monkeypatch.setitem(wakeword._BUILDERS, "openwakeword", lambda c: tocado.append(c))
    monkeypatch.delenv("PICOVOICE_ACCESS_KEY", raising=False)

    assert isinstance(build_wake_word(_config(engine="whisper")), NullWakeWord)
    assert isinstance(build_wake_word(_config(engine="porcupine")), NullWakeWord)
    assert not tocado


def test_porcupine_sin_ppn_no_usa_keywords_integrados(monkeypatch):
    monkeypatch.setenv("PICOVOICE_ACCESS_KEY", "clave")
    usado = []
    monkeypatch.setitem(wakeword._BUILDERS, "whisper", lambda c: usado.append("whisper") or NullWakeWord())

    build_wake_word(_config(engine="porcupine", keyword_path=""))

    assert usado == ["whisper"]


def test_oye_las_variantes_de_corrin_y_no_otras_palabras():
    match = _spotter()._matches
    for oido in ["Corrin", "¡Corrin!", "Corin.", "Córin", "Corrin, abre Spotify"]:
        assert match(oido), f"no detectó «{oido}»"
    for oido in ["Hey Jarvis", "Nova", "hola qué tal", "Gracias por ver el video",
                 "Mi hermano corrió", "Corren por el parque", "Vamos a correr", "Cortina roja"]:
        assert not match(oido), f"falso positivo con «{oido}»"


def test_la_pista_no_basta_para_despertar():
    """Con pista Whisper escribe «Corrin» donde se dijo «corrió»; sin ella, no."""
    class _Segment:
        def __init__(self, text):
            self.text = text

    def spotter_que_oye(sin_pista):
        spotter = _spotter()

        def transcribe(audio, initial_prompt=None, **_):
            return [_Segment("Corrin" if initial_prompt else sin_pista)], None

        spotter._model.transcribe = transcribe
        return spotter

    for falso in ["Mi hermano corrió", "corren por el parque", "por el parque", ""]:
        assert spotter_que_oye(falso).process(_voice(120)) is False, falso
    for real in ["Corrin", "Corin", "Coring", "corrin abre spotify"]:
        assert spotter_que_oye(real).process(_voice(120)) is True, real


def test_tras_detectar_no_se_vuelve_a_despertar_con_el_solape():
    spotter = _spotter(heard="Corrin")
    assert spotter.process(_voice(120)) is True
    assert len(spotter._buffer) == 0
    assert spotter.process(_voice(60)) is False       # no llega a una ventana


def test_el_bufer_no_crece_si_el_detector_se_atrasa():
    spotter = _spotter()
    for _ in range(50):
        spotter.process(_voice(300))
    assert len(spotter._buffer) <= spotter._window_bytes


def _sesion_con_detector(procesar):
    class _Wake:
        blocking = True

        def process(self, audio):
            return procesar(audio)

    sesion = VoiceSession.__new__(VoiceSession)
    sesion._wake = _Wake()
    sesion._wake_task = None
    sesion._wake_pending = bytearray()
    sesion.woke = 0

    async def _on_wake():
        sesion.woke += 1

    sesion._on_wake_detected = _on_wake
    return sesion


def test_no_se_pierde_audio_mientras_el_detector_decodifica():
    """Las tramas que llegaban con una decodificación en vuelo se tiraban, y
    cada ventana tenía huecos que podían partir la palabra."""
    recibido = bytearray()

    def procesar(audio):
        recibido.extend(audio)
        return False

    async def main():
        sesion = _sesion_con_detector(procesar)
        trama = b"\x01\x02" * (FRAME_BYTES // 2)
        for _ in range(50):
            await sesion._handle_idle(trama)      # sin ceder: el hilo sigue ocupado
        await sesion._wake_task
        await sesion._handle_idle(trama)
        await sesion._wake_task
        return trama

    trama = asyncio.run(main())
    assert len(recibido) == 51 * len(trama)


def test_al_despertar_no_quedan_tramas_viejas():
    async def main():
        sesion = _sesion_con_detector(lambda _audio: True)
        await sesion._handle_idle(b"\0" * FRAME_BYTES)
        await sesion._handle_idle(b"\0" * FRAME_BYTES)
        await sesion._wake_task
        return sesion

    sesion = asyncio.run(main())
    assert sesion.woke == 1 and not sesion._wake_pending



def test_dicha_sola_basta_un_parecido_menor():
    """Pasó con la voz real: con pista «Corrin.», sin pista «corten» y se rechazaba."""
    class _Segment:
        def __init__(self, text):
            self.text = text

    def spotter_que_oye(con_pista, sin_pista):
        spotter = _spotter()

        def transcribe(audio, initial_prompt=None, **_):
            return [_Segment(con_pista if initial_prompt else sin_pista)], None

        spotter._model.transcribe = transcribe
        return spotter

    assert spotter_que_oye("Corrin.", "corten").process(_voice(120)) is True
    assert spotter_que_oye("Oye Corrin", "corten").process(_voice(120)) is True
    # Dentro de una frase se sigue exigiendo más:
    assert spotter_que_oye("Miermano Corrin.", "mi hermano corten").process(_voice(120)) is False
    # Y lo que no se parece nada, ni sola:
    assert spotter_que_oye("Corrin.", "por el parque").process(_voice(120)) is False
