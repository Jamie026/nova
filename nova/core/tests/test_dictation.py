"""Dictar un correo o un nombre letra por letra, y enviar sólo tras un «sí»."""
import asyncio

import pytest

from nova.agent import spelling
from nova.audio.format import FRAME_BYTES
from nova.audio.vad import VadEvent
from nova.session import VoiceSession
from nova.tools import dictation_tools


@pytest.mark.parametrize("oido, esperado", [
    ("jota u a ene arroba gmail punto com", "juan@gmail.com"),
    ("Jota, U, A, Ene, arroba, Gmail, punto, com.", "juan@gmail.com"),
    ("J. U. A. N. arroba gmail punto com", "juan@gmail.com"),
    ("J-U-A-N arroba hotmail punto es", "juan@hotmail.es"),
    ("juan@gmail.com", "juan@gmail.com"),
    ("be larga e ele e ene arroba yahoo punto com", "belen@yahoo.com"),
    ("be de burro o be arroba gmail punto com", "bob@gmail.com"),
    ("ve chica i ce te o ere guion bajo dos tres arroba gmail punto com", "victor_23@gmail.com"),
    ("doble u a ele te e ere arroba gmail punto com", "walter@gmail.com"),
    ("i griega o ele a guion ere arroba outlook punto com", "yola-r@outlook.com"),
    ("ge a ere ce i a mayúscula arroba gmail punto com", "garcia@gmail.com"),
    ("eme a ere i a uno nueve ocho cinco arroba gmail punto com", "maria1985@gmail.com"),
])
def test_decodifica_correos(oido, esperado):
    assert spelling.decode(oido, "email") == esperado


@pytest.mark.parametrize("oido, esperado", [
    ("a ene a", "Ana"),
    ("A, N, A.", "Ana"),
    ("a ele a i ene espacio ere i a ene", "Alain Rian"),
    ("eñe o ere a", "Ñora"),
    ("jota o ese e", "Jose"),
])
def test_decodifica_nombres(oido, esperado):
    assert spelling.decode(oido, "name") == esperado


def test_repite_el_dato_para_poder_comprobarlo():
    assert spelling.read_back("ana@gmail.com") == "a, n, a, arroba, gmail punto com"
    assert spelling.read_back("Ana Li") == "A, n, a, espacio, L, i"


def correr(tool, dictados, respuestas):
    dictados, respuestas = list(dictados), list(respuestas)
    pedidos, preguntas = [], []

    async def dictar(prompt):
        pedidos.append(prompt)
        return dictados.pop(0)

    async def confirmar(_id, texto, _riesgo):
        preguntas.append(texto)
        return respuestas.pop(0)

    spell_out = dictation_tools.build(dictar, confirmar)[0]
    return asyncio.run(spell_out.handler(**tool)), pedidos, preguntas


def test_devuelve_lo_que_el_usuario_confirmo():
    r, pedidos, preguntas = correr({"kind": "email", "label": "el correo de Juan"},
                                   ["jota u a ene arroba gmail punto com"], [True])
    assert r.ok and r.data["value"] == "juan@gmail.com"
    assert "el correo de Juan" in pedidos[0]
    assert "j, u, a, n, arroba" in preguntas[0]


def test_si_dice_que_no_lo_vuelve_a_pedir():
    r, pedidos, _ = correr({"kind": "name"}, ["a ene a", "a ene e"], [False, True])
    assert r.ok and r.data["value"] == "Ane"
    assert len(pedidos) == 2


def test_lo_que_no_es_un_correo_no_se_da_por_bueno():
    r, pedidos, preguntas = correr({"kind": "email"},
                                   ["no sé qué", "jota arroba gmail punto com"], [True])
    assert r.ok and r.data["value"] == "j@gmail.com"
    assert len(pedidos) == 2 and len(preguntas) == 1
    assert "no te entendí" in pedidos[1]


def test_se_rinde_a_los_tres_intentos():
    r, pedidos, _ = correr({"kind": "email"}, ["nada", "nada", "nada"], [])
    assert not r.ok and len(pedidos) == 3


def test_se_puede_cancelar_y_el_silencio_no_inventa_nada():
    assert not correr({"kind": "name"}, ["cancela"], [])[0].ok
    assert not correr({"kind": "name"}, [""], [])[0].ok


# ------------------------------------------------------------------- sesión
class Vad:
    def __init__(self):
        self.eventos = []

    def reset(self):
        pass

    def push(self, _frame):
        return self.eventos.pop(0) if self.eventos else VadEvent.NONE


class Stt:
    def __init__(self):
        self.prompt = None

    async def transcribe(self, audio, final=True, strict=True, prompt=None):
        self.prompt, self.strict = prompt, strict
        return type("T", (), {"text": "jota u a ene"})()


class Ipc:
    async def broadcast(self, _event):
        pass


class Agente:
    async def translate_for_speech(self, texto):
        return texto


class Mic:
    def drain(self):
        pass


def sesion():
    import collections

    s = VoiceSession.__new__(VoiceSession)
    s._dictation_vad, s._stt, s._ipc, s._agent, s._mic = Vad(), Stt(), Ipc(), Agente(), Mic()
    s._capturing, s._utterance, s._dictation = False, bytearray(), None
    s._preroll = collections.deque(maxlen=40)
    s.dicho = []

    async def hablar(texto, **_):
        s.dicho.append(texto)

    s._speak = hablar
    return s


def test_el_dictado_aguanta_pausas_y_usa_su_vocabulario():
    async def main():
        s = sesion()
        pedido = asyncio.create_task(s.request_dictation("Dime el nombre letra por letra."))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        trama = b"\x01" * FRAME_BYTES
        s._dictation_vad.eventos = [VadEvent.SPEECH_START] + [VadEvent.NONE] * 200 + [VadEvent.SPEECH_END]
        for _ in range(202):
            await s._handle_dictation_audio(trama)
        return s, await pedido

    s, texto = asyncio.run(main())
    assert texto == "jota u a ene"
    assert s.dicho == ["Dime el nombre letra por letra."]
    assert s._stt.prompt == spelling.SPELLING_PROMPT and s._stt.strict is False
    assert s._dictation is None


def test_la_pausa_del_dictado_es_mas_larga_que_la_normal():
    from nova.config import DEFAULTS

    assert DEFAULTS["session"]["dictation_pause_ms"] > 2 * DEFAULTS["vad"]["end_ms"]
