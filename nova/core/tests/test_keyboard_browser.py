"""Navegar con el navegador que el usuario ya tiene abierto, por teclado."""
import pytest

from nova.browser import keyboard as kb
from nova.os_adapter.base import WindowInfo
from nova.tools import browser_tools


class AdaptadorFalso:
    def __init__(self, ventanas):
        self.ventanas = ventanas
        self.acciones = []

    def list_windows(self):
        return self.ventanas

    def focus_window(self, h):
        self.acciones.append(("focus", h)); return True

    def press_keys(self, keys):
        self.acciones.append(("keys", tuple(keys))); return True

    def type_text(self, t):
        self.acciones.append(("type", t)); return True

    def get_clipboard(self):
        return "lo que tenía el usuario"

    def set_clipboard(self, t):
        self.acciones.append(("clip", t)); return True


OPERA = WindowInfo(handle=3, title="Acceso Rápido - Opera", process="opera.exe", foreground=True)
DISCORD = WindowInfo(handle=7, title="@I'mRyan - Discord", process="Discord.exe")


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    async def nada(_s): return None
    monkeypatch.setattr(kb.asyncio, "sleep", nada)


def test_encuentra_el_navegador_abierto_por_el_titulo():
    assert kb.find_browser_window(AdaptadorFalso([DISCORD, OPERA])).title.endswith("Opera")
    assert kb.find_browser_window(AdaptadorFalso([DISCORD])) is None


def test_si_hay_varios_gana_el_preferido():
    chrome = WindowInfo(handle=9, title="YouTube - Google Chrome", process="chrome.exe", foreground=True)
    assert kb.find_browser_window(AdaptadorFalso([chrome, OPERA]), prefer="opera").handle == 3
    assert kb.find_browser_window(AdaptadorFalso([chrome, OPERA])).handle == 9   # el de primer plano


@pytest.mark.asyncio
async def test_abre_youtube_en_la_pestana_actual():
    # Lo que pidió el usuario: la URL en la pestaña que está mirando, no otra.
    ad = AdaptadorFalso([OPERA])
    await kb.KeyboardBrowser(ad).goto("youtube.com")
    # Pegada, no tecleada (teclear perdía el principio si la ventana aún se
    # estaba restaurando), y el portapapeles del usuario vuelve a su sitio.
    assert ad.acciones == [("focus", 3), ("keys", ("ctrl", "l")), ("clip", "https://youtube.com"),
                           ("keys", ("ctrl", "a")), ("keys", ("ctrl", "v")),
                           ("clip", "lo que tenía el usuario"), ("keys", ("enter",))]


@pytest.mark.asyncio
async def test_pestana_nueva_solo_si_se_pide():
    ad = AdaptadorFalso([OPERA])
    await kb.KeyboardBrowser(ad).goto("https://gmail.com", new_tab=True)
    assert ("keys", ("ctrl", "t")) in ad.acciones and ("keys", ("ctrl", "l")) not in ad.acciones


@pytest.mark.asyncio
async def test_las_herramientas_usan_el_navegador_del_usuario_si_esta_abierto():
    ad = AdaptadorFalso([OPERA])
    tools = {t.name: t for t in browser_tools.build(browser=None, os_adapter=ad, mode="auto")}
    r = await tools["browser_goto"].handler(url="youtube.com")
    assert r.ok and "tu navegador" in r.summary
    assert ("clip", "https://youtube.com") in ad.acciones


@pytest.mark.asyncio
async def test_lo_que_necesita_cdp_lo_dice_y_apunta_a_read_screen():
    ad = AdaptadorFalso([OPERA])
    tools = {t.name: t for t in browser_tools.build(browser=None, os_adapter=ad, mode="auto")}
    r = await tools["browser_read_page"].handler()
    assert not r.ok and "read_screen" in r.summary
    assert ad.acciones == []


def test_encuentra_opera_aunque_el_titulo_no_lleve_firma_o_este_minimizada():
    # Opera GX titula la ventana sólo con la página; y minimizada sigue valiendo.
    sin_firma = WindowInfo(handle=9, title="Comprar cuentas Zenless Zone Zero | Venta", process="opera.exe", minimized=True)
    assert kb.find_browser_window(AdaptadorFalso([DISCORD, sin_firma])).handle == 9
    # Pero una visible gana a una minimizada.
    visible = WindowInfo(handle=10, title="YouTube", process="opera.exe")
    assert kb.find_browser_window(AdaptadorFalso([sin_firma, visible])).handle == 10


async def test_sin_navegador_lanza_el_preferido_en_vez_de_chrome():
    class Lanzador(AdaptadorFalso):
        def __init__(self):
            super().__init__([DISCORD]); self.lanzado = None
        def find_app(self, q):
            return q if q == "Opera GX" else None
        def open_app(self, app):
            self.lanzado = app; self.ventanas = [DISCORD, OPERA]; return True

    ad = Lanzador()
    b = kb.KeyboardBrowser(ad, prefer="opera")
    assert (await b.ensure()).title.endswith("Opera")
    assert ad.lanzado == "Opera GX"


async def test_sin_preferido_no_lanza_nada():
    ad = AdaptadorFalso([DISCORD])
    assert await kb.KeyboardBrowser(ad, prefer="").ensure() is None
