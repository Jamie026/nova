"""La carga del modelo de voz tiene que ser de un solo vuelo.

El fallo real: el arranque carga en segundo plano (un minuto) y la primera frase
del usuario llegaba antes. `speak()` veía el modelo a None y lanzaba OTRA carga:
dos copias de 1,8 GB en una GPU de 6 GB, Electron sin memoria (avatar en blanco)
y Whisper a 7 s por frase. Se prueba sin modelo: lo que importa es cuántas veces
se llama a la carga, no lo que carga.
"""
import asyncio

import numpy as np
import pytest

from nova.tts.xtts_engine import XttsEngine


class MotorFalso(XttsEngine):
    def __init__(self):
        super().__init__(reference="no-importa.wav", language="en")
        self.cargas = 0
        self.sintesis = 0

    def _load(self):
        self.cargas += 1
        import time
        time.sleep(0.15)          # como una carga de verdad, pero corta
        self._latents = ("gpt", "spk")
        return object()

    def _synthesize(self, text, language=None):
        self.sintesis += 1
        return np.zeros(2400, dtype=np.float32)


@pytest.mark.asyncio
async def test_dos_warmups_a_la_vez_cargan_una_sola_vez():
    motor = MotorFalso()
    await asyncio.gather(motor.warmup(), motor.warmup(), motor.warmup())
    assert motor.cargas == 1
    assert motor._model is not None


@pytest.mark.asyncio
async def test_hablar_durante_la_carga_espera_en_vez_de_recargar():
    motor = MotorFalso()
    fondo = asyncio.create_task(motor.warmup())     # lo que hace el arranque
    await asyncio.sleep(0.02)                        # la carga ya va en marcha
    # El usuario habla antes de que termine: speak() no debe cargar de nuevo.
    import nova.tts.xtts_engine as X

    class SinAltavoz:
        def __init__(s, *a, **k): pass
        def start(s): pass
        def write(s, p): pass
        def stop(s): pass
        def abort(s): pass
        def close(s): pass
    X.sd.OutputStream = SinAltavoz

    assert await motor.speak("Hello there.") is True
    await fondo
    assert motor.cargas == 1


@pytest.mark.asyncio
async def test_un_warmup_posterior_no_vuelve_a_cargar():
    motor = MotorFalso()
    await motor.warmup()
    await motor.warmup()
    assert motor.cargas == 1
