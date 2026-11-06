"""Mientras Nova envía en segundo plano, sigue despierta y lo cuenta."""
import asyncio
import collections
import time

import pytest

from nova.session import VoiceSession
from nova.state import AgentState, StateMachine


class Cfg:
    def __init__(self, dormirse=True):
        self.valores = {"session.follow_up_seconds": 12, "session.sleep_on_idle": dormirse}

    def get(self, key, default=None):
        return self.valores.get(key, default)


class Nada:
    def reset(self):
        pass

    def drain(self):
        pass


class Ipc:
    async def broadcast(self, _e):
        pass


def sesion(estado=AgentState.IDLE):
    s = VoiceSession.__new__(VoiceSession)
    s._machine = StateMachine(estado)
    s._capturing, s._confirm_id, s._dictation, s._keep_awake = False, None, None, False
    s._follow_up_until = 0.0
    s._config, s._vad, s._mic, s._ipc = Cfg(), Nada(), Nada(), Ipc()
    s._preroll = collections.deque(maxlen=40)
    s._utterance, s._side, s._side_capturing = bytearray(), bytearray(), False
    s.dicho = []

    async def hablar(texto, **_):
        s.dicho.append((texto, s._machine.state))
        return True

    s._speak = hablar
    return s


def test_esta_libre_solo_si_nadie_la_esta_usando():
    s = sesion(AgentState.LISTENING)
    assert s.is_free()
    s._capturing = True
    assert not s.is_free()                       # el usuario está hablando
    s._capturing, s._confirm_id = False, "abc"
    assert not s.is_free()                       # espera un «sí»
    s._confirm_id = None
    for ocupada in (AgentState.THINKING, AgentState.EXECUTING, AgentState.SPEAKING):
        s._machine = StateMachine(ocupada)
        assert not s.is_free()


def test_no_se_duerme_mientras_envia_y_si_al_terminar():
    async def main():
        s = sesion(AgentState.IDLE)
        await s.keep_awake(True)
        assert s._machine.state is AgentState.LISTENING     # se despierta sola
        await s._check_follow_up_timeout()
        assert s._machine.state is AgentState.LISTENING     # y no se duerme
        await s.keep_awake(False)
        await s._check_follow_up_timeout()
        assert s._machine.state is AgentState.LISTENING     # aún dentro de la ventana normal
        s._follow_up_until = time.monotonic() - 1
        await s._check_follow_up_timeout()
        return s._machine.state

    assert asyncio.run(main()) is AgentState.IDLE


def test_los_avisos_se_dicen_hablando_y_vuelve_a_escuchar():
    async def main():
        s = sesion(AgentState.LISTENING)
        s._keep_awake = True
        await s.announce("Ya mandé las 2 facturas")
        return s

    s = asyncio.run(main())
    assert s.dicho == [("Ya mandé las 2 facturas", AgentState.SPEAKING)]   # en SPEAKING: su eco no es una orden
    assert s._machine.state is AgentState.LISTENING


def test_un_aviso_no_pisa_una_conversacion():
    async def main():
        s = sesion(AgentState.THINKING)
        await s.announce("Ya mandé las 2 facturas")
        return s

    s = asyncio.run(main())
    assert s.dicho == [] and s._machine.state is AgentState.THINKING


def test_el_lote_avisa_cuando_envia_y_cuenta_el_avance(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from openpyxl import Workbook

    from nova.tools import messaging_tools as mt

    monkeypatch.setattr(mt, "input_enabled", lambda: True)
    libro = Workbook()
    libro.active.append(["Tel"])
    for n in ("900000001", "900000002"):
        libro.active.append([n])
    excel = tmp_path / "l.xlsx"
    libro.save(excel)
    actividad, avisos = [], []

    async def activo(a):
        actividad.append(a)

    async def avisar(t, speak=False):
        avisos.append((t, speak))

    async def enviar(_p, _t):
        return "ok", ""

    tools = {t.name: t for t in mt.build(
        None, searcher=SimpleNamespace(allows=lambda _p: True), data_dir=tmp_path, notify=avisar,
        sender=enviar, on_activity=activo,
        settings={"country_code": "51", "gap_s": [0, 0], "warn_s": 0, "batch_pause_min": 0,
                  "batch_size": 1})}                       # dos tandas: hay una pausa en medio

    async def main():
        await tools["whatsapp_batch"].handler(path=str(excel), phone_column="Tel", template="Hola")
        await asyncio.wait_for(asyncio.gather(*[t for t in asyncio.all_tasks()
                                                if t.get_name() == "whatsapp-batch"]), 5)

    asyncio.run(main())
    # Despierta desde el primer envío hasta el final, sin dormirse a medias.
    assert actividad[0] is True and actividad[-1] is False and False not in actividad[:-1]
    textos = [t for t, _ in avisos]
    assert "Mandando 1 de 2…" in textos and "Mandando 2 de 2…" in textos
    assert any(hablado and "no toques el ratón" in t for t, hablado in avisos)
    assert any(hablado and t.startswith("¡Listo!") for t, hablado in avisos)


def test_por_defecto_avisa_10_segundos_antes():
    from nova.config import DEFAULTS
    from nova.tools.messaging_tools import BATCH_DEFAULTS

    assert DEFAULTS["messaging"]["warn_s"] == BATCH_DEFAULTS["warn_s"] == 10


def test_por_defecto_no_se_duerme_sola_solo_con_descansa_o_el_boton():
    from nova.config import DEFAULTS

    assert DEFAULTS["session"]["sleep_on_idle"] is False

    async def main():
        s = sesion(AgentState.LISTENING)
        s._config = Cfg(dormirse=False)
        s._follow_up_until = time.monotonic() - 3600          # una hora sin que nadie le hable
        await s._check_follow_up_timeout()
        despierta = s._machine.state
        s._cancel_turn = lambda: asyncio.sleep(0)
        await s.interrupt()                                   # el botón de interrumpir
        return despierta, s._machine.state

    despierta, tras_boton = asyncio.run(main())
    assert despierta is AgentState.LISTENING
    assert tras_boton is AgentState.IDLE


def test_entre_mensajes_siempre_5_segundos():
    from nova.config import DEFAULTS
    from nova.tools.messaging_tools import BATCH_DEFAULTS

    assert DEFAULTS["messaging"]["gap_s"] == BATCH_DEFAULTS["gap_s"] == [5, 5]
    assert DEFAULTS["messaging"]["gap_large_s"] == BATCH_DEFAULTS["gap_large_s"] == [10, 10]
    assert DEFAULTS["messaging"]["large_batch"] == BATCH_DEFAULTS["large_batch"] == 20


@pytest.mark.parametrize("cuantos, pausa", [(2, 5.0), (20, 5.0), (21, 10.0)])
def test_la_pausa_depende_de_cuantos_mensajes(tmp_path, monkeypatch, cuantos, pausa):
    from types import SimpleNamespace

    from openpyxl import Workbook

    from nova.tools import messaging_tools as mt

    monkeypatch.setattr(mt, "input_enabled", lambda: True)
    libro = Workbook()
    libro.active.append(["Tel"])
    for i in range(cuantos):
        libro.active.append([f"9000{i:05d}"])
    excel = tmp_path / "l.xlsx"
    libro.save(excel)
    esperas = []

    def medir(a, b):
        esperas.append((a, b))
        return 0.0

    monkeypatch.setattr(mt.random, "uniform", medir)

    async def enviar(_p, _t):
        return "ok", ""

    tools = {t.name: t for t in mt.build(
        None, searcher=SimpleNamespace(allows=lambda _p: True), data_dir=tmp_path, sender=enviar,
        settings={"country_code": "51", "warn_s": 0, "batch_pause_min": 0, "batch_size": 100})}

    async def main():
        await tools["whatsapp_batch"].handler(path=str(excel), phone_column="Tel", template="Hola")
        await asyncio.wait_for(asyncio.gather(*[t for t in asyncio.all_tasks()
                                                if t.get_name() == "whatsapp-batch"]), 10)

    asyncio.run(main())
    assert esperas and set(esperas) == {(pausa, pausa)}
