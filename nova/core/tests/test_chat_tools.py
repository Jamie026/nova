"""Abrir un chat por nombre y mandar un mensaje, sin tocar ningún Discord real.

Lo que se prueba es la coreografía: qué teclas se pulsan, en qué orden, y qué se
escribe. El adaptador falso lo anota todo.
"""
import asyncio

import pytest

from nova.os_adapter.base import WindowInfo
from nova.tools import control_tools


class AdaptadorFalso:
    """Discord de mentira: Ctrl+K, un nombre y Enter cambian el título de la
    ventana a «<nombre> - Discord», como hace el de verdad. Si el nombre no está
    en `conocidos`, el título no cambia: es lo que pasa cuando el buscador no
    encuentra a nadie."""
    name = "falso"

    def __init__(self, ventanas, conocidos=("Alain Ryan",)):
        self._ventanas = ventanas
        self._conocidos = conocidos
        self.acciones: list[tuple] = []
        self._buscando = None

    def list_windows(self):
        return self._ventanas

    def focus_window(self, handle):
        self.acciones.append(("focus", handle))
        return True

    def press_keys(self, keys):
        self.acciones.append(("keys", tuple(keys)))
        if tuple(keys) == ("ctrl", "k"):
            self._buscando = ""
        elif tuple(keys) == ("enter",) and self._buscando is not None:
            if self._buscando in self._conocidos:
                v = self._ventanas[0]
                self._ventanas[0] = WindowInfo(handle=v.handle, title=f"{self._buscando} - Discord",
                                               process=v.process, foreground=True)
            self._buscando = None
        return True

    def type_text(self, text):
        self.acciones.append(("type", text))
        if self._buscando is not None:
            self._buscando = text
        return True

    def screen_size(self):
        return (1920, 1080)


class BuscadorFalso:
    def allows(self, path):
        return True


def _herramientas(ventanas, **kw):
    adaptador = AdaptadorFalso(ventanas, **kw)
    tools = {t.name: t for t in control_tools.build(adaptador, BuscadorFalso())}
    return adaptador, tools


def DISCORD():
    return [WindowInfo(handle=7, title="Amigos - Discord", process="Discord.exe", foreground=True)]


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    # Las pausas existen para que la app real procese; aquí sólo estorban.
    async def nada(_s):
        return None
    monkeypatch.setattr(control_tools.asyncio, "sleep", nada)
    monkeypatch.delenv("NOVA_NO_INPUT", raising=False)


@pytest.mark.asyncio
async def test_open_chat_usa_el_buscador_rapido_de_discord():
    adaptador, tools = _herramientas(DISCORD())
    resultado = await tools["open_chat"].handler(app="Discord", chat="Alain Ryan")
    assert resultado.ok
    assert adaptador.acciones == [
        ("focus", 7),
        ("keys", ("ctrl", "k")),
        ("type", "Alain Ryan"),
        ("keys", ("enter",)),
    ]


@pytest.mark.asyncio
async def test_send_message_abre_el_chat_escribe_y_envia_en_un_solo_paso():
    adaptador, tools = _herramientas(DISCORD())
    resultado = await tools["send_message"].handler(
        app="Discord", chat="Alain Ryan", text="Hola, ¿qué tal?")
    assert resultado.ok and resultado.data["sent"] is True
    assert adaptador.acciones == [
        ("focus", 7),
        ("keys", ("ctrl", "k")),
        ("type", "Alain Ryan"),
        ("keys", ("enter",)),          # abre el chat
        ("type", "Hola, ¿qué tal?"),
        ("keys", ("enter",)),          # envía
    ]


@pytest.mark.asyncio
async def test_send_message_sin_enviar_no_pulsa_enter_al_final():
    adaptador, tools = _herramientas(
        [WindowInfo(handle=7, title="Alain Ryan - Discord", process="Discord.exe", foreground=True)])
    resultado = await tools["send_message"].handler(
        app="Discord", text="borrador", send=False)
    assert resultado.ok and resultado.data["sent"] is False
    assert adaptador.acciones == [("focus", 7), ("type", "borrador")]


@pytest.mark.asyncio
async def test_send_message_en_el_chat_ya_abierto_no_toca_el_buscador():
    adaptador, tools = _herramientas(
        [WindowInfo(handle=7, title="Alain Ryan - Discord", process="Discord.exe", foreground=True)])
    await tools["send_message"].handler(app="Discord", text="ok")
    assert ("keys", ("ctrl", "k")) not in adaptador.acciones


@pytest.mark.asyncio
async def test_no_teclea_en_la_pantalla_de_amigos():
    # Lo que pasó de verdad: «hola» + Enter en «Amigos - Discord», y la
    # herramienta dijo «lo envié». Ahora se niega y pide el destinatario.
    adaptador, tools = _herramientas(DISCORD())
    resultado = await tools["send_message"].handler(app="Discord", text="hola")
    assert not resultado.ok and "Dime a quién" in resultado.summary
    assert ("type", "hola") not in adaptador.acciones


@pytest.mark.asyncio
async def test_si_el_buscador_no_encuentra_a_nadie_lo_dice():
    adaptador, tools = _herramientas(DISCORD(), conocidos=())
    resultado = await tools["send_message"].handler(app="Discord", chat="Nadie Conocido", text="hola")
    assert not resultado.ok and "No encontré" in resultado.summary
    assert ("type", "hola") not in adaptador.acciones    # no escribió el mensaje al aire


def test_reconoce_que_pantallas_no_son_un_chat():
    from nova.tools.control_tools import looks_like_chat, title_matches_chat

    assert not looks_like_chat("Amigos - Discord", "Discord")
    assert not looks_like_chat("Friends - Discord", "Discord")
    assert not looks_like_chat("Discord", "Discord")
    assert looks_like_chat("Alain Ryan - Discord", "Discord")
    assert looks_like_chat("#general - Mi Servidor - Discord", "Discord")
    assert title_matches_chat("Alain Ryan - Discord", "alain ryan")
    assert title_matches_chat("Alain - Discord", "Alain Ryan")      # apodo
    assert not title_matches_chat("Amigos - Discord", "Alain Ryan")


@pytest.mark.asyncio
async def test_si_la_app_no_esta_abierta_lo_dice_en_vez_de_teclear_al_aire():
    adaptador, tools = _herramientas([WindowInfo(handle=1, title="Bloc de notas", process="notepad.exe")])
    resultado = await tools["send_message"].handler(app="Discord", chat="Alain", text="hola")
    assert not resultado.ok and "Ábrela primero" in resultado.summary
    assert adaptador.acciones == []


@pytest.mark.asyncio
async def test_app_sin_buscador_conocido_no_improvisa():
    adaptador, tools = _herramientas([WindowInfo(handle=3, title="WhatsApp", process="WhatsApp.exe")])
    resultado = await tools["open_chat"].handler(app="WhatsApp", chat="Mamá")
    assert not resultado.ok and "No sé cómo" in resultado.summary
    assert adaptador.acciones == []


def test_la_confirmacion_lleva_mensaje_destinatario_y_app():
    _, tools = _herramientas(DISCORD())
    texto = tools["send_message"].confirmation_text(
        {"app": "Discord", "chat": "Alain Ryan", "text": "nos vemos a las 8"})
    assert texto == "¿Envío «nos vemos a las 8» a «Alain Ryan» en Discord?"
    borrador = tools["send_message"].confirmation_text(
        {"app": "Discord", "text": "hola", "send": False})
    assert borrador == "¿Escribo «hola» en Discord sin enviarlo?"


def test_enviar_mensajes_es_de_riesgo_alto():
    from nova.tools.registry import Risk

    _, tools = _herramientas(DISCORD())
    assert tools["send_message"].risk is Risk.HIGH      # enviar no se deshace
    assert tools["open_chat"].risk is Risk.MEDIUM       # abrir otro chat, sí


@pytest.mark.asyncio
async def test_con_el_teclado_desactivado_no_hace_nada(monkeypatch):
    monkeypatch.setenv("NOVA_NO_INPUT", "1")
    adaptador, tools = _herramientas(DISCORD())
    resultado = await tools["send_message"].handler(app="Discord", chat="x", text="hola")
    assert not resultado.ok
    assert adaptador.acciones == []
