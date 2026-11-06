"""Correo y WhatsApp: dejar el texto listo, sin duplicar pestañas y sin enviar de más."""
import asyncio
import re
import sys
from urllib.parse import parse_qs, urlparse

import pytest

from nova.browser import keyboard as kb
from nova.capture.ocr import ScreenLine, ScreenReading
from nova.os_adapter.base import AppInfo, TabInfo, WindowInfo
from nova.tools import messaging_tools as mt
from nova.tools.registry import ToolRegistry

OPERA = WindowInfo(handle=3, title="GitHub - Opera", process="opera.exe", foreground=True)
SIZE = (0, 0, 1920, 1020)


def linea(texto, x, y):
    return ScreenLine(0, texto, x - 40, y - 8, 80, 16)


class Pantalla:
    """El OCR falso: cada lectura devuelve la siguiente, y la última se repite."""

    def __init__(self, *lecturas):
        self.lecturas = list(lecturas)

    def leer(self):
        lineas = self.lecturas.pop(0) if len(self.lecturas) > 1 else self.lecturas[0]
        return ScreenReading(list(lineas), (0, 0), (1920, 1020), "w", 0.0)


class Os:
    def __init__(self, ventanas=(OPERA,), pestanas=None, app=None):
        self.ventanas = list(ventanas)
        self.pestanas = pestanas or {}
        self.app = app
        self.acciones = []
        self.clip = ""

    def list_windows(self):
        return self.ventanas

    def focus_window(self, h):
        self.acciones.append(("focus", h)); return True

    def browser_tabs(self, h):
        return self.pestanas.get(h, [])

    def select_tab(self, h, index):
        self.acciones.append(("tab", h, index)); return True

    def press_keys(self, keys):
        self.acciones.append(("keys", tuple(keys))); return True

    def type_text(self, t):
        self.acciones.append(("type", t)); return True

    def click(self, x, y, button="left", double=False):
        self.acciones.append(("click", x, y)); return True

    def copy_files_to_clipboard(self, rutas):
        self.acciones.append(("archivo", rutas[0])); return True

    portapapeles = "del usuario"

    def get_clipboard(self):
        return self.portapapeles

    def set_clipboard(self, t):
        self.acciones.append(("clip", t)); self.portapapeles = t; return True

    def window_rect(self, h):
        return SIZE

    def find_app(self, q):
        return self.app

    def open_app(self, app):
        self.acciones.append(("open_app", app.name))
        self.ventanas.append(WindowInfo(9, "WhatsApp", "WhatsApp.exe"))
        return True

    def open_path(self, p):
        self.acciones.append(("open_path", p))
        self.ventanas.append(OPERA)
        return True

    def teclas(self):
        return [a[1] for a in self.acciones if a[0] == "keys"]

    def urls(self):
        return [a[1] for a in self.acciones if a[0] == "clip" and a[1].startswith("http")]


@pytest.fixture(autouse=True)
def _rapido(monkeypatch):
    async def nada(_s): return None
    monkeypatch.setattr(asyncio, "sleep", nada)
    monkeypatch.setattr(mt.ocr, "available", lambda: True)
    monkeypatch.setattr(mt, "input_enabled", lambda: True)
    monkeypatch.setattr(mt, "_READY_TIMEOUT_S", 0.0)
    monkeypatch.setattr(kb, "_TITLE_WAIT_S", 0.0)


def herramientas(os_falso, pantalla=None, monkeypatch=None):
    if pantalla is not None:
        async def leer(box, title=""):
            return pantalla.leer()
        monkeypatch.setattr(mt.ocr, "read_screen", leer)
    return {t.name: t for t in mt.build(os_falso)}


def correr(tool, **args):
    return asyncio.run(tool.handler(**args))


# ------------------------------------------------------------------- pestañas
@pytest.mark.parametrize("titulo", ["Gmail", "Recibidos (3) - ana@gmail.com - Gmail",
                                    "Compose Mail - ana@gmail.com - Gmail"])
def test_reconoce_gmail(titulo):
    assert mt.is_gmail_tab(titulo)


@pytest.mark.parametrize("titulo", ["Cómo usar Gmail - YouTube", "gmail - Buscar con Google"])
def test_no_confunde_paginas_sobre_gmail(titulo):
    assert not mt.is_gmail_tab(titulo)


@pytest.mark.parametrize("titulo", ["WhatsApp", "(1) WhatsApp", "(12) WhatsApp", "WhatsApp Web"])
def test_reconoce_whatsapp(titulo):
    assert mt.is_whatsapp_tab(titulo)


@pytest.mark.parametrize("titulo", ["whatsapp - Buscar con Google", "Descargar WhatsApp para Windows"])
def test_no_confunde_paginas_sobre_whatsapp(titulo):
    assert not mt.is_whatsapp_tab(titulo)


# --------------------------------------------------------------------- correo
def test_el_correo_reutiliza_la_pestana_de_gmail_y_no_abre_otra():
    os_falso = Os(pestanas={3: [TabInfo(0, "GitHub", True),
                                TabInfo(1, "Recibidos - ana@gmail.com - Gmail", False)]})
    r = correr(herramientas(os_falso)["compose_email"], to="juan@x.com", subject="Hola", body="Qué tal")
    assert r.ok and r.data["reused_tab"] and not r.data["sent"]
    assert ("tab", 3, 1) in os_falso.acciones
    assert ("ctrl", "t") not in os_falso.teclas()
    query = parse_qs(urlparse(os_falso.urls()[0]).query)
    assert query["to"] == ["juan@x.com"] and query["su"] == ["Hola"] and query["body"] == ["Qué tal"]
    assert query["authuser"] == ["ana@gmail.com"]     # la cuenta de esa pestaña


def test_el_correo_sin_pestana_abre_una_nueva_sin_pisar_la_actual():
    os_falso = Os(pestanas={3: [TabInfo(0, "GitHub", True)]})
    r = correr(herramientas(os_falso)["compose_email"], to="juan@x.com")
    assert r.ok and not r.data["reused_tab"]
    assert ("ctrl", "t") in os_falso.teclas()


def test_el_correo_sin_navegador_usa_el_navegador_del_sistema():
    os_falso = Os(ventanas=[])
    r = correr(herramientas(os_falso)["compose_email"], to="juan@x.com")
    assert r.ok
    assert os_falso.acciones[0][0] == "open_path"
    assert os_falso.acciones[0][1].startswith(mt.GMAIL_COMPOSE)


@pytest.mark.parametrize("direccion", ["juan", "juan arroba gmail", "juan@", ""])
def test_no_inventa_direcciones(direccion):
    os_falso = Os()
    r = correr(herramientas(os_falso)["compose_email"], to=direccion)
    assert not r.ok and "dirección" in r.summary
    assert not os_falso.acciones


def test_el_correo_nunca_pulsa_enter_en_la_pagina():
    os_falso = Os(pestanas={3: [TabInfo(0, "Gmail", True)]})
    correr(herramientas(os_falso)["compose_email"], to="juan@x.com")
    assert os_falso.teclas().count(("enter",)) == 1     # el de la barra de direcciones
    assert "send" not in {t.name for t in mt.build(os_falso)}


# ------------------------------------------------------------------- whatsapp
BUSCADOR = linea("Q Buscar un chat o iniciar uno nuevo", 285, 243)
INICIO = [BUSCADOR, linea("web.whatsapp.com", 193, 70)]
CAJA = linea("Escribe un mensaje", 867, 972)


def resultados(*nombres):
    return [linea(n, 200, 400 + 80 * i) for i, n in enumerate(nombres)]


def chat(nombre, caja=CAJA, *extra):
    return [BUSCADOR, linea(nombre, 700, 170), *( [caja] if caja else [] ), *extra]


def tab_whatsapp():
    return Os(pestanas={3: [TabInfo(0, "GitHub", True), TabInfo(1, "(1) WhatsApp", False)]})


def borrador(monkeypatch, os_falso, *lecturas, nombre="Ana", texto="Hola"):
    tools = herramientas(os_falso, Pantalla(*lecturas), monkeypatch)
    return tools, correr(tools["whatsapp_draft"], chat=nombre, text=texto)


def test_deja_el_mensaje_escrito_sin_pulsar_enter(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana María"), chat("Ana María"))
    assert r.ok and r.data["chat"] == "Ana María" and not r.data["sent"]
    assert ("tab", 3, 1) in os_falso.acciones          # la pestaña que ya había
    assert ("ctrl", "t") not in os_falso.teclas()
    assert ("type", "Hola") in os_falso.acciones
    assert ("enter",) not in os_falso.teclas()


def test_los_saltos_de_linea_no_envian_el_mensaje(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Ana"), texto="Hola\nqué\r\ntal")
    assert r.ok
    assert ("type", "Hola qué tal") in os_falso.acciones


def test_sin_pestana_abre_whatsapp_web_en_una_nueva(monkeypatch):
    os_falso = Os(pestanas={3: [TabInfo(0, "GitHub", True)]})
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Ana"))
    assert r.ok
    assert ("ctrl", "t") in os_falso.teclas()
    assert os_falso.urls() == [mt.WHATSAPP_WEB]


def test_la_app_instalada_gana_a_la_web(monkeypatch):
    os_falso = Os(pestanas={3: [TabInfo(0, "(1) WhatsApp", True)]},
                  app=AppInfo("WhatsApp", "whatsapp!app"))
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Ana"))
    assert r.ok and "app" in r.data["via"]
    assert ("open_app", "WhatsApp") in os_falso.acciones
    assert not os_falso.urls()


def test_otra_app_con_nombre_parecido_no_cuenta_como_whatsapp(monkeypatch):
    os_falso = tab_whatsapp()
    os_falso.app = AppInfo("WhatCha", "x")
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Ana"))
    assert r.ok and r.data["via"] == "WhatsApp Web"


def test_contacto_inexistente_no_escribe_nada_y_un_solo_esc(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, [linea("No se encontró ningún chat", 370, 749)])
    assert not r.ok and "No encontré" in r.summary
    assert os_falso.teclas().count(("esc",)) == 1
    assert ("type", "Hola") not in os_falso.acciones


def test_ana_no_es_mariana(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Mariana López", "hola Ana qué tal"))
    assert not r.ok
    assert ("type", "Hola") not in os_falso.acciones


def test_varios_contactos_posibles_se_pregunta(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana García", "Ana López"))
    assert not r.ok and "Ana García" in r.summary and "Ana López" in r.summary
    assert ("type", "Hola") not in os_falso.acciones


def test_si_el_chat_abierto_no_es_el_pedido_no_escribe(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Pedro"))
    assert not r.ok and "no escribí nada" in r.summary
    assert ("type", "Hola") not in os_falso.acciones


def test_un_borrador_previo_en_la_caja_no_se_toca(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Ana", None))
    assert not r.ok
    assert ("type", "Hola") not in os_falso.acciones


def test_sin_sesion_lo_dice(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, [linea("Vincular dispositivo", 900, 500)])
    assert not r.ok and "iniciar sesión" in r.summary


# -------------------------------------------------------------------- enviar
ESCRITO = linea("Hola", 700, 972)


def preparado(monkeypatch, *al_enviar):
    os_falso = tab_whatsapp()
    tools, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana María"), chat("Ana María"))
    assert r.ok
    herramientas_pantalla = Pantalla(*al_enviar)

    async def leer(box, title=""):
        return herramientas_pantalla.leer()

    monkeypatch.setattr(mt.ocr, "read_screen", leer)
    return os_falso, tools


def test_enviar_pregunta_siempre_y_nombra_al_contacto_leido(monkeypatch):
    os_falso, tools = preparado(monkeypatch, chat("Ana María", None, ESCRITO))
    tool = tools["whatsapp_send"]
    registry = ToolRegistry(risk_policy={"low": "auto", "medium": "auto", "high": "auto"},
                            auto_approve=["whatsapp_send"])
    assert registry.needs_confirmation(tool, {})
    assert tool.confirmation_text({}) == "¿Le mando «Hola» a Ana María por WhatsApp?"


def test_enviar_pulsa_enter_si_todo_cuadra(monkeypatch):
    os_falso, tools = preparado(monkeypatch, chat("Ana María", None, ESCRITO), chat("Ana María"))
    r = correr(tools["whatsapp_send"])
    assert r.ok and r.data["sent"]
    assert ("enter",) in os_falso.teclas()


def test_no_envia_si_cambiaron_de_chat(monkeypatch):
    os_falso, tools = preparado(monkeypatch, chat("Pedro", None, ESCRITO))
    r = correr(tools["whatsapp_send"])
    assert not r.ok
    assert ("enter",) not in os_falso.teclas()


def test_no_envia_si_el_texto_ya_no_esta(monkeypatch):
    os_falso, tools = preparado(monkeypatch, chat("Ana María"))
    r = correr(tools["whatsapp_send"])
    assert not r.ok and "vacía" in r.summary
    assert ("enter",) not in os_falso.teclas()


def test_no_envia_si_en_la_caja_pone_otra_cosa(monkeypatch):
    os_falso, tools = preparado(monkeypatch, chat("Ana María", None, linea("zzzzzz", 700, 972)))
    r = correr(tools["whatsapp_send"])
    assert not r.ok
    assert ("enter",) not in os_falso.teclas()


def test_enviar_sin_borrador_no_hace_nada():
    os_falso = tab_whatsapp()
    r = correr(herramientas(os_falso)["whatsapp_send"])
    assert not r.ok
    assert not os_falso.acciones


def test_un_mensaje_no_se_envia_dos_veces(monkeypatch):
    os_falso, tools = preparado(monkeypatch, chat("Ana María", None, ESCRITO), chat("Ana María"))
    assert correr(tools["whatsapp_send"]).ok
    assert not correr(tools["whatsapp_send"]).ok
    assert os_falso.teclas().count(("enter",)) == 1


def test_el_aviso_de_enviado_no_miente(monkeypatch):
    os_falso, tools = preparado(monkeypatch, chat("Ana María", None, ESCRITO))
    r = correr(tools["whatsapp_send"])
    assert r.ok and "no pude comprobar" in r.summary
    assert not re.search(r"^Envié", r.summary)


# ------------------------------------------------------------- enviar correo
GMAIL_TAB = [TabInfo(0, "Compose Mail - ana@gmail.com - Gmail", True)]
ASUNTO = linea("Reunión del lunes", 500, 200)
BOTON = linea("Enviar", 120, 900)


def correo_preparado(monkeypatch, *al_enviar, **args):
    os_falso = Os(pestanas={3: GMAIL_TAB})
    pantalla = Pantalla(*al_enviar)
    tools = herramientas(os_falso, pantalla, monkeypatch)
    r = correr(tools["compose_email"], to="juan@x.com", **(args or {"subject": "Reunión del lunes"}))
    assert r.ok
    return os_falso, tools


def clicks(os_falso):
    return [a[1:] for a in os_falso.acciones if a[0] == "click"]


def test_enviar_correo_pregunta_siempre_con_destinatario_y_asunto(monkeypatch):
    _, tools = correo_preparado(monkeypatch, [ASUNTO, BOTON])
    tool = tools["email_send"]
    registry = ToolRegistry(risk_policy={"low": "auto", "medium": "auto", "high": "auto"},
                            auto_approve=["email_send"])
    assert registry.needs_confirmation(tool, {})
    assert tool.confirmation_text({}) == "¿Le mando el correo a juan@x.com, el de «Reunión del lunes»?"


def test_tras_el_si_pulsa_enviar(monkeypatch):
    os_falso, tools = correo_preparado(monkeypatch, [ASUNTO, BOTON], [linea("Mensaje enviado", 100, 980)])
    r = correr(tools["email_send"])
    assert r.ok and r.data["sent"]
    assert BOTON.center in clicks(os_falso)


def test_sin_boton_visible_usa_el_atajo_de_gmail(monkeypatch):
    os_falso, tools = correo_preparado(monkeypatch, [ASUNTO], [])
    assert correr(tools["email_send"]).ok
    assert ("ctrl", "enter") in os_falso.teclas()


def test_no_envia_si_el_borrador_ya_no_esta_en_pantalla(monkeypatch):
    os_falso, tools = correo_preparado(monkeypatch, [linea("Recibidos", 100, 200), BOTON])
    r = correr(tools["email_send"])
    assert not r.ok and "no envié" in r.summary
    assert not clicks(os_falso) and ("ctrl", "enter") not in os_falso.teclas()


def test_no_envia_un_correo_vacio(monkeypatch):
    os_falso, tools = correo_preparado(monkeypatch, [BOTON], body=" ")
    r = correr(tools["email_send"])
    assert not r.ok and "vacío" in r.summary
    assert not clicks(os_falso)


def test_no_envia_si_cerraron_la_pestana(monkeypatch):
    os_falso, tools = correo_preparado(monkeypatch, [ASUNTO, BOTON])
    os_falso.pestanas = {3: [TabInfo(0, "GitHub", True)]}
    r = correr(tools["email_send"])
    assert not r.ok and "no envié" in r.summary
    assert not clicks(os_falso) and not os_falso.urls()[1:]


def test_sin_compose_no_envia_nada():
    os_falso = Os(pestanas={3: GMAIL_TAB})
    assert not correr(herramientas(os_falso)["email_send"]).ok
    assert not os_falso.acciones


def test_un_correo_no_se_envia_dos_veces(monkeypatch):
    os_falso, tools = correo_preparado(monkeypatch, [ASUNTO, BOTON], [])
    assert correr(tools["email_send"]).ok
    assert not correr(tools["email_send"]).ok
    assert clicks(os_falso).count(BOTON.center) == 1


def test_si_no_se_entiende_el_dato_se_pide_dictado(monkeypatch):
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, [linea("No se encontró", 370, 749)])
    assert "spell_out" in r.summary
    assert "spell_out" in correr(herramientas(Os())["compose_email"], to="juan").summary


# ---------------------------------------------------- pestañas reales de Windows
@pytest.mark.skipif(sys.platform != "win32", reason="sólo Windows")
def test_el_adaptador_de_windows_convierte_las_pestanas(monkeypatch):
    """El except de browser_tabs convertía cualquier error en «no hay pestañas»."""
    from types import SimpleNamespace as Ns

    from nova.os_adapter.windows import WindowsAdapter

    adaptador = WindowsAdapter.__new__(WindowsAdapter)
    elementos = [(Ns(CurrentName="GitHub"), Ns(CurrentIsSelected=0)),
                 (Ns(CurrentName="(1) WhatsApp"), Ns(CurrentIsSelected=1))]
    monkeypatch.setattr(adaptador, "_tab_elements", lambda _h: elementos, raising=False)
    assert adaptador.browser_tabs(1) == [TabInfo(0, "GitHub", False), TabInfo(1, "(1) WhatsApp", True)]


# ------------------------------------------------------------ por número
def test_por_numero_reutiliza_la_pestana_y_no_envia(monkeypatch):
    os_falso = tab_whatsapp()
    escrito = linea("Hola Ana", 700, 972)
    tools = herramientas(os_falso, Pantalla([BUSCADOR, linea("Ana Torres", 700, 170), escrito]), monkeypatch)
    r = correr(tools["whatsapp_draft"], phone="987 654 321", country_code="51", text="Hola Ana\nSaludos")
    assert r.ok and r.data["phone"] == "51987654321" and not r.data["sent"]
    url = os_falso.urls()[0]
    assert url.startswith(mt.WHATSAPP_WEB + "send?") and "phone=51987654321" in url
    assert "Hola+Ana%0ASaludos" in url                 # el salto viaja como salto, no como Enter
    assert ("tab", 3, 1) in os_falso.acciones and ("ctrl", "t") not in os_falso.teclas()
    assert os_falso.teclas().count(("enter",)) == 1   # el único Enter es el de la barra
    assert tools["whatsapp_send"].confirmation_text({}) == \
        "¿Le mando «Hola Ana\nSaludos» a Ana Torres, al +51987654321, por WhatsApp?"


def test_por_numero_sin_whatsapp_lo_dice(monkeypatch):
    os_falso = tab_whatsapp()
    aviso = linea("El número de teléfono compartido a través de la dirección URL no es válido", 900, 500)
    tools = herramientas(os_falso, Pantalla([aviso]), monkeypatch)
    r = correr(tools["whatsapp_draft"], phone="+51 987 654 321", text="Hola")
    assert not r.ok and "no tiene WhatsApp" in r.summary


def test_por_numero_sin_codigo_de_pais_pregunta(monkeypatch):
    os_falso = tab_whatsapp()
    r = correr(herramientas(os_falso)["whatsapp_draft"], phone="987654321", text="Hola")
    assert not r.ok and "country_code" in r.summary
    assert not os_falso.acciones


def test_lo_escrito_en_el_buscador_no_cuenta_como_resultado(monkeypatch):
    """Con RapidOCR, «zzqxv» escrito en la caja se leía un poco por debajo de la
    etiqueta y se tomaba por un chat encontrado."""
    os_falso = tab_whatsapp()
    _, r = borrador(monkeypatch, os_falso, INICIO, [BUSCADOR, linea("zzqxv", 200, 250)], nombre="zzqxv")
    assert not r.ok and "No encontré" in r.summary
    assert not [a for a in os_falso.acciones if a[0] == "click"][1:]   # sólo el clic en la caja



# ------------------------------------------------------ enviar un archivo
CAJA_VACIA = linea("Escribe un mensaje", 867, 972)


def previa(nombre):
    """WhatsApp Web tras pegar un archivo: su nombre y la caja de comentario más
    arriba que la normal (0,82 de alto frente a 0,95), como se midió."""
    return [BUSCADOR, linea(nombre, 1300, 245), linea("1 página", 1300, 265),
            linea("Escribe un mensaje", 1000, 836)]


def facturas_con_adjunto(monkeypatch, tmp_path, *lecturas, template=""):
    import json
    from types import SimpleNamespace

    from openpyxl import Workbook

    carpeta = tmp_path / "facturas"
    carpeta.mkdir()
    factura = carpeta / "Factura F001-000101.pdf"
    factura.write_bytes(b"f1")
    libro = Workbook()
    libro.active.append(["Nombre", "Teléfono"])
    libro.active.append(["Ana Torres Vega", "952046749"])
    excel = tmp_path / "clientes.xlsx"
    libro.save(excel)

    pantalla = Pantalla(*lecturas)

    async def leer(box, title=""):
        return pantalla.leer()

    monkeypatch.setattr(mt.ocr, "read_screen", leer)
    os_falso = tab_whatsapp()
    datos = {"cliente": "TORRES VEGA ANA", "monto": "350.00", "moneda": "S/", "factura": "F001-000101"}

    async def modelo(_m):
        return SimpleNamespace(text=json.dumps(datos))

    tools = {t.name: t for t in mt.build(
        os_falso, searcher=SimpleNamespace(allows=lambda _p: True), data_dir=tmp_path,
        llm_chat=modelo, read_text=lambda _r: (["FACTURA F001-000101", "TOTAL S/ 350.00"], [0.99, 0.99]),
        settings={"country_code": "51", "gap_s": [0, 0], "warn_s": 0, "batch_pause_min": 0})}

    async def main():
        r = await tools["process_invoices"].handler(path=str(carpeta), clients=str(excel),
                                                     template=template, attach=True)
        assert r.ok, r.summary
        await asyncio.wait_for(asyncio.gather(*[t for t in asyncio.all_tasks()
                                                if t.get_name() == "whatsapp-batch"]), 5)
        return await tools["whatsapp_batch_status"].handler()

    return os_falso, asyncio.run(main()), factura


CONTROL = linea("Nova 6749", 700, 972)        # el texto de control que trae la dirección


def test_manda_la_factura_misma_con_su_comentario(monkeypatch, tmp_path):
    os_falso, estado, factura = facturas_con_adjunto(
        monkeypatch, tmp_path, [BUSCADOR, CONTROL], [BUSCADOR, CAJA_VACIA], previa("Factura F001-000101.pdf"),
        [BUSCADOR, CAJA_VACIA], template="Hola {Nombre}, te adjunto tu factura")
    assert "1 enviados de 1" in estado.summary
    assert "phone=51952046749" in os_falso.urls()[0] and "text=Nova+6749" in os_falso.urls()[0]
    assert ("archivo", str(factura)) in os_falso.acciones
    assert ("type", "Hola Ana Torres Vega, te adjunto tu factura") in os_falso.acciones
    teclas = os_falso.teclas()
    assert teclas.index(("ctrl", "v")) < len(teclas) - 1 - teclas[::-1].index(("enter",))


def test_sin_comentario_solo_va_el_archivo(monkeypatch, tmp_path):
    os_falso, estado, _ = facturas_con_adjunto(
        monkeypatch, tmp_path, [BUSCADOR, CONTROL], [BUSCADOR, CAJA_VACIA], previa("Factura F001-000101.pdf"),
        [BUSCADOR, CAJA_VACIA])
    assert "1 enviados de 1" in estado.summary
    assert not [a for a in os_falso.acciones if a[0] == "type"]


def test_si_no_aparece_la_vista_previa_no_se_pulsa_enter(monkeypatch, tmp_path):
    monkeypatch.setattr(mt, "_PREVIEW_TIMEOUT_S", 0.0)
    os_falso, estado, _ = facturas_con_adjunto(monkeypatch, tmp_path, [BUSCADOR, CONTROL], [BUSCADOR, CAJA_VACIA])
    assert "1 fallidos" in estado.summary and "no mostró el archivo" in estado.summary
    teclas = os_falso.teclas()
    pegar_archivo = len(teclas) - 1 - teclas[::-1].index(("ctrl", "v"))   # el último: el primero es la dirección
    assert ("enter",) not in teclas[pegar_archivo:]


# ------------------------------------------- pestañas sin accesibilidad (Opera)
class OperaCiega(Os):
    """La barra de pestañas no se puede leer; Ctrl+N cambia de pestaña y el
    título de la ventana dice cuál es, como en un navegador de verdad."""

    def __init__(self, titulos, activa=0):
        super().__init__(ventanas=[])
        self.titulos, self.activa = titulos, activa

    def browser_tabs(self, h):
        return []

    def list_windows(self):
        return [WindowInfo(3, f"{self.titulos[self.activa]} - Opera", "opera.exe", foreground=True)]

    def press_keys(self, keys):
        if keys[0] == "ctrl" and keys[1].isdigit():
            self.activa = min(int(keys[1]), len(self.titulos)) - 1
        return super().press_keys(keys)


def test_sin_accesibilidad_encuentra_la_pestana_recorriendolas():
    opera = OperaCiega(["GitHub", "YouTube", "(1) WhatsApp", "Gmail"])
    from nova.browser.keyboard import KeyboardBrowser

    visita = asyncio.run(KeyboardBrowser(opera).open_site(mt.WHATSAPP_WEB, mt.is_whatsapp_tab))
    assert visita.reused and opera.activa == 2
    assert ("ctrl", "t") not in opera.teclas()


def test_sin_accesibilidad_y_sin_whatsapp_vuelve_a_tu_pestana_y_abre_otra():
    opera = OperaCiega(["GitHub", "YouTube", "Gmail"], activa=1)
    from nova.browser.keyboard import KeyboardBrowser

    visita = asyncio.run(KeyboardBrowser(opera).open_site(mt.WHATSAPP_WEB, mt.is_whatsapp_tab))
    assert not visita.reused
    teclas = opera.teclas()
    vuelta = teclas[teclas.index(("ctrl", "t")) - 1]
    assert vuelta == ("ctrl", "2")                     # volvió a YouTube antes de abrir la nueva


def test_cambia_a_la_pestana_pedida_sin_abrir_otra():
    from nova.tools import browser_tools

    opera = OperaCiega(["SellSuite", "YouTube", "(1) WhatsApp"], activa=0)
    tools = {t.name: t for t in browser_tools.build(browser=None, os_adapter=opera, mode="keyboard")}
    r = asyncio.run(tools["browser_switch_tab"].handler(match="whatsapp"))
    assert r.ok and opera.activa == 2
    assert ("ctrl", "t") not in opera.teclas()
    lista = asyncio.run(tools["browser_list_tabs"].handler())
    assert lista.ok and "3. (1) WhatsApp ←" in lista.summary and opera.activa == 2   # vuelve a la tuya
    r = asyncio.run(tools["browser_switch_tab"].handler(match="Gmail"))
    assert not r.ok and "browser_goto" in r.summary and opera.activa == 2


# ---------------------------------------------- Zoom u otra ventana encima
ZOOM = WindowInfo(99, "Zoom Workplace", "Zoom.exe")


class Tapada(Os):
    """`tapado(x, y)` dice si en ese punto se ve Zoom en vez de Opera."""

    def __init__(self, tapado, **kw):
        super().__init__(pestanas={3: [TabInfo(0, "GitHub", True), TabInfo(1, "(1) WhatsApp", False)]}, **kw)
        self.ventanas = [OPERA, ZOOM]
        self.tapado = tapado

    def window_at(self, x, y):
        return 99 if self.tapado(x, y) else 3


def test_si_zoom_tapa_el_centro_hace_clic_en_otro_punto_de_la_caja(monkeypatch):
    os_falso = Tapada(lambda x, y: abs(x - BUSCADOR.center[0]) < 30)       # el vídeo de Zoom encima
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Ana"))
    assert r.ok
    clics = [a[1:] for a in os_falso.acciones if a[0] == "click"]
    assert BUSCADOR.center not in clics                                     # ahí estaba Zoom
    assert all(not os_falso.tapado(*c) for c in clics)


def test_si_zoom_tapa_toda_la_caja_no_hace_clic_y_lo_dice(monkeypatch):
    os_falso = Tapada(lambda x, y: True)
    _, r = borrador(monkeypatch, os_falso, INICIO)
    assert not r.ok and "Zoom Workplace" in r.summary and "tapando WhatsApp" in r.summary
    assert not [a for a in os_falso.acciones if a[0] == "click"]
    assert ("type", "Hola") not in os_falso.acciones



def test_si_zoom_tapa_el_texto_hace_clic_mas_a_la_derecha_en_la_misma_caja(monkeypatch):
    """Lo que pasó con Zoom compartiendo: su barra tapaba «Escribe un mensaje» y la
    mitad izquierda de la caja, pero no el final (en la prueba real, x > 1550)."""
    os_falso = Tapada(lambda x, y: y > 900 and x < 1550)
    _, r = borrador(monkeypatch, os_falso, INICIO, resultados("Ana"), chat("Ana"))
    assert r.ok
    clic_caja = [a[1:] for a in os_falso.acciones if a[0] == "click" and a[2] > 900]
    assert clic_caja and all(1550 <= x <= int(1920 * 0.86) for x, _ in clic_caja)



def test_si_la_direccion_no_carga_no_pega_el_archivo_en_el_chat_de_otro(monkeypatch, tmp_path):
    """Pasó de verdad: la dirección del número no cargó, WhatsApp siguió en el
    último chat abierto (Kiara) con la caja vacía, y la factura se pegó ahí."""
    monkeypatch.setattr(mt, "_PHONE_TIMEOUT_S", 0.0)
    os_falso, estado, _ = facturas_con_adjunto(
        monkeypatch, tmp_path, [BUSCADOR, linea("Kiara", 700, 170), CAJA_VACIA])   # sin el texto de control
    assert "1 fallidos" in estado.summary
    assert not [a for a in os_falso.acciones if a[0] == "archivo"]          # ni se copió el archivo
    assert os_falso.teclas().count(("enter",)) == 1                        # sólo el de la barra de direcciones
