"""Abrir una aplicación que ya está abierta la trae al frente, no abre otra."""
import asyncio

from nova.os_adapter.base import AppInfo, WindowInfo
from nova.tools.applications import build, running_window


class Os:
    def __init__(self, ventanas, app=None):
        self.ventanas, self.app, self.acciones = ventanas, app, []

    def list_windows(self):
        return self.ventanas

    def find_app(self, q):
        return self.app

    def focus_window(self, h):
        self.acciones.append(("focus", h)); return True

    def open_app(self, app):
        self.acciones.append(("open", app.name)); return True


OPERA_MIN = WindowInfo(5, "WhatsApp - Opera", "opera.exe", minimized=True)
CHROME_SPOTIFY = WindowInfo(6, "Spotify - Web Player - Google Chrome", "chrome.exe")


def abrir(os_falso, **args):
    tool = {t.name: t for t in build(os_falso)}["open_application"]
    return asyncio.run(tool.handler(**args))


def test_opera_minimizada_se_trae_al_frente():
    os_falso = Os([OPERA_MIN], AppInfo("Navegador Opera GX", "opera"))
    r = abrir(os_falso, application="Opera")
    assert r.ok and r.data["already_open"]
    assert os_falso.acciones == [("focus", 5)]


def test_si_no_esta_abierta_se_abre():
    os_falso = Os([], AppInfo("Spotify", "spotify"))
    assert abrir(os_falso, application="Spotify").ok
    assert os_falso.acciones == [("open", "Spotify")]


def test_una_pestana_con_ese_nombre_no_es_la_aplicacion():
    os_falso = Os([CHROME_SPOTIFY], AppInfo("Spotify", "spotify"))
    abrir(os_falso, application="Spotify")
    assert os_falso.acciones == [("open", "Spotify")]


def test_otra_ventana_solo_si_se_pide():
    os_falso = Os([OPERA_MIN], AppInfo("Navegador Opera GX", "opera"))
    abrir(os_falso, application="Opera", new_window=True)
    assert os_falso.acciones == [("open", "Navegador Opera GX")]


def test_reconoce_procesos_con_otro_nombre():
    code = WindowInfo(7, "nova - Visual Studio Code", "Code.exe")
    edge = WindowInfo(8, "Bing - Microsoft Edge", "msedge.exe")
    assert running_window(Os([code]), "visual studio code") is code
    assert running_window(Os([edge]), "Microsoft Edge") is edge
