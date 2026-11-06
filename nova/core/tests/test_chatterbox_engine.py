"""El motor de voz Chatterbox sin GPU: planificación de la tubería, juez de cortas, cancelación."""
import asyncio

import numpy as np

from nova.tts import chatterbox_engine as cb
from nova.tts.chatterbox_engine import ChatterboxEngine


def _engine(tomas, oidos=()):
    eng = ChatterboxEngine("ref.wav")
    eng._model = object()
    tomas = list(tomas); oidos = list(oidos)

    def synth(text, stats=None):
        if stats is not None:
            stats.update(tokens=10, gen_s=0.1)
        return tomas.pop(0)
    eng._synthesize = synth

    async def verifier(pcm, lang):
        return oidos.pop(0)
    eng.verifier = verifier if oidos else None
    return eng


def test_sin_retraso_con_oraciones_parecidas():
    eng = ChatterboxEngine("ref.wav")
    eng._rtf = 0.55
    assert eng._initial_delay(["Una oración de tamaño normal para hablar."] * 3) == 0.0


def test_retraso_si_la_primera_es_corta_y_la_segunda_larga():
    eng = ChatterboxEngine("ref.wav")
    eng._rtf = 0.55
    corta = "Done, that is it."
    larga = "Here is what I found in your downloads folder: forty-two files, mostly images and PDFs from last month, and one video."
    delay = eng._initial_delay([corta, larga])
    # La larga tarda 0,55·dur+0,3 en estar lista; la corta dura ~1,3 s: hay que esperar la diferencia.
    esperado = (0.55 * (len(larga) / 19) + 0.3) - max(0.8, len(corta) / 19)   # lo que tarda la larga menos lo que dura la corta
    assert abs(delay - esperado) < 0.01 and delay > 0.5


async def test_una_corta_mala_se_repite_y_una_larga_no_se_comprueba():
    mala = np.zeros(2400, np.float32); buena = np.ones(2400, np.float32) * 0.1
    eng = _engine([mala, buena], ["tch tch", "Done."])
    assert (await eng._take("Done.")) is buena
    larga = np.ones(100, np.float32)
    eng2 = _engine([larga], ["lo que sea"])
    assert (await eng2._take("A sentence long enough not to be checked at all, really.")) is larga


async def test_la_cancelacion_para_la_respuesta(monkeypatch):
    eng = ChatterboxEngine("ref.wav"); eng._model = object()
    llamadas = []

    def synth(text, stats=None):
        llamadas.append(text); return np.ones(24000, np.float32) * 0.05
    eng._synthesize = synth

    import time

    class SinAltavoz:
        def __init__(s, *a, **k): pass
        def start(s): pass
        def write(s, p): time.sleep(0.01)      # como un altavoz: a tiempo real, más o menos
        def stop(s): pass
        def abort(s): pass
        def close(s): pass
    monkeypatch.setattr(cb.sd, "OutputStream", SinAltavoz)
    task = asyncio.create_task(eng.speak("First sentence here, quite normal. Second sentence here, also normal. Third one to finish."))
    await asyncio.sleep(0.05)
    await eng.stop()
    assert await task is False
