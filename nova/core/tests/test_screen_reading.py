"""Leer la pantalla como texto y hacer clic en lo leído, sin OCR ni ratón reales."""
import time

import pytest

from nova.capture.ocr import ScreenLine, ScreenReading
from nova.os_adapter.base import WindowInfo
from nova.tools import screen_tools


def _lectura(lineas, origen=(100, 50)):
    ls = [ScreenLine(i + 1, t, origen[0] + x, origen[1] + y, 80, 16) for i, (t, x, y) in enumerate(lineas)]
    return ScreenReading(ls, origen, (800, 600), "Discord", time.monotonic())


def test_el_modelo_ve_coordenadas_relativas_a_la_ventana():
    texto = _lectura([("Mensajes directos", 10, 300), ("I'mRyan", 60, 340), ("Kiawa", 60, 380)]).render()
    assert "Ventana «Discord», 800x600" in texto
    assert "1. 10,300 «Mensajes directos»" in texto
    assert "3. 60,380 «Kiawa»" in texto


def test_no_se_pasa_del_tope_de_lineas():
    lectura = _lectura([(f"línea {i}", 0, i * 20) for i in range(200)])
    texto = lectura.render(limit=120)
    assert "120. " in texto and "121. " not in texto and "80 líneas más" in texto


class AdaptadorFalso:
    def __init__(self):
        self.clics = []

    def list_windows(self):
        return [WindowInfo(handle=1, title="Discord", process="Discord.exe", foreground=True)]

    def focus_window(self, h):
        return True

    def window_rect(self, h):
        return (100, 50, 900, 650)

    def click(self, x, y, button="left", double=False):
        self.clics.append((x, y))
        return True

    def scroll(self, x, y, clicks, horizontal=False):
        self.ruedas = getattr(self, "ruedas", []) + [(x, y, clicks, horizontal)]
        return True

    def screen_size(self):
        return (1920, 1080)


def _tools(monkeypatch, lectura):
    adaptador = AdaptadorFalso()
    tools = {t.name: t for t in screen_tools.build(adaptador, capture=None)}
    monkeypatch.setattr(screen_tools.ocr, "available", lambda: True)

    async def ocr_falso(box=None, title="pantalla", language="es"):
        return lectura
    monkeypatch.setattr(screen_tools.ocr, "read_screen", ocr_falso)

    async def nada(_s):
        return None
    monkeypatch.setattr(screen_tools.asyncio, "sleep", nada)
    return adaptador, tools


@pytest.mark.asyncio
async def test_leer_y_hacer_clic_en_el_centro_de_la_linea(monkeypatch):
    lectura = _lectura([("Mensajes directos", 10, 300), ("I'mRyan", 60, 340), ("Kiawa", 60, 380)])
    adaptador, tools = _tools(monkeypatch, lectura)
    leido = await tools["read_screen"].handler(window="Discord")
    assert leido.ok and leido.data["lines"] == 3
    hecho = await tools["click_item"].handler(index=3, text="Kiawa")
    assert hecho.ok
    # centro: x = 100+60 + 80/2, y = 50+380 + 16/2
    assert adaptador.clics == [(200, 438)]


@pytest.mark.asyncio
async def test_no_hace_clic_si_el_texto_no_es_el_que_el_modelo_esperaba(monkeypatch):
    adaptador, tools = _tools(monkeypatch, _lectura([("Kiawa", 60, 380)]))
    await tools["read_screen"].handler()
    r = await tools["click_item"].handler(index=1, text="Alain Ryan")
    assert not r.ok and "Vuelve a leer" in r.summary
    assert adaptador.clics == []


@pytest.mark.asyncio
async def test_no_hace_clic_sin_lectura_previa_ni_con_lectura_vieja(monkeypatch):
    adaptador, tools = _tools(monkeypatch, _lectura([("Kiawa", 60, 380)]))
    r = await tools["click_item"].handler(index=1, text="Kiawa")
    assert not r.ok and "read_screen primero" in r.summary

    await tools["read_screen"].handler()
    reloj = time.monotonic          # el módulo `time` es compartido: guardar el original
    monkeypatch.setattr(screen_tools.time, "monotonic", lambda: reloj() + 60)
    r = await tools["click_item"].handler(index=1, text="Kiawa")
    assert not r.ok
    assert adaptador.clics == []


def test_riesgos():
    from nova.tools.registry import Risk
    tools = {t.name: t for t in screen_tools.build(AdaptadorFalso(), capture=None)}
    assert tools["read_screen"].risk is Risk.MEDIUM     # el texto de tu pantalla va al modelo
    assert tools["click_item"].risk is Risk.MEDIUM      # clic sobre texto leído: navegación
    assert tools["click_item"].confirmation_text({"index": 3, "text": "Kiawa"}) == "¿Hago clic en «Kiawa»?"


@pytest.mark.asyncio
async def test_scroll_en_el_centro_de_la_ventana_y_la_lectura_caduca(monkeypatch):
    adaptador, tools = _tools(monkeypatch, _lectura([("Kiawa", 60, 380)]))
    await tools["read_screen"].handler(window="Discord")
    r = await tools["scroll_window"].handler(direction="down", amount=4, window="Discord")
    assert r.ok and "abajo" in r.summary and "read_screen" in r.summary
    # centro de (100,50)-(900,650); abajo = muescas negativas
    assert adaptador.ruedas == [(500, 350, -4, False)]
    # lo leído ya no está donde estaba: click_item obliga a releer
    r = await tools["click_item"].handler(index=1, text="Kiawa")
    assert not r.ok and "read_screen primero" in r.summary


@pytest.mark.asyncio
async def test_scroll_sin_ventana_va_a_la_que_esta_delante(monkeypatch):
    adaptador, tools = _tools(monkeypatch, _lectura([]))
    r = await tools["scroll_window"].handler(direction="up", amount=2)
    assert r.ok and adaptador.ruedas == [(500, 350, 2, False)]
    r = await tools["scroll_window"].handler(direction="right")
    assert adaptador.ruedas[-1] == (500, 350, 5, True)
    r = await tools["scroll_window"].handler(direction="diagonal")
    assert not r.ok
