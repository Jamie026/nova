"""Fallos encontrados en la auditoría."""
import asyncio
import socket

import pytest
import websockets

from nova.files.search import FileSearcher
from nova.ipc.server import IpcServer
from nova.tools import file_tools


def _puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_una_pagina_web_no_puede_mandarle_ordenes_a_nova():
    """Una página abierta en el navegador podía conectarse a 127.0.0.1 y enviar
    «text_input». Ahora se rechaza cualquier conexión que traiga cabecera Origin."""
    async def main():
        puerto = _puerto_libre()
        servidor = IpcServer("127.0.0.1", puerto)
        recibido = []

        async def apuntar(m):
            recibido.append(m)

        servidor.on("text_input", apuntar)
        await servidor.start()
        try:
            with pytest.raises(websockets.exceptions.InvalidHandshake):      # 403
                async with websockets.connect(f"ws://127.0.0.1:{puerto}", origin="https://pagina-cualquiera.com") as ws:
                    await ws.send('{"type": "text_input", "text": "manda un whatsapp"}')
            # La app (cliente sin Origin) sí entra.
            async with websockets.connect(f"ws://127.0.0.1:{puerto}") as ws:
                await ws.recv()                                     # «ready»
                await ws.send('{"type": "text_input", "text": "hola"}')
                await asyncio.sleep(0.2)
        finally:
            await servidor.stop()
        return recibido

    assert asyncio.run(main()) == [{"type": "text_input", "text": "hola"}]


def test_la_busqueda_da_la_ruta_completa_y_las_relativas_se_resuelven(tmp_path):
    carpeta = tmp_path / "Descargas" / "Prueba Nova"
    carpeta.mkdir(parents=True)
    (carpeta / "clientes.xlsx").write_bytes(b"x")
    buscador = FileSearcher([tmp_path / "Descargas"])
    tool = {t.name: t for t in file_tools.build(None, buscador)}["find_files"]
    r = asyncio.run(tool.handler(name="clientes"))
    assert str(carpeta / "clientes.xlsx") in r.summary
    assert buscador.resolve(r"Prueba Nova\clientes.xlsx") == carpeta / "clientes.xlsx"
    assert buscador.resolve("clientes.xlsx") == carpeta / "clientes.xlsx"         # un nivel más abajo
    assert buscador.resolve(str(carpeta / "x.pdf")) == carpeta / "x.pdf"          # absoluta: tal cual


def test_un_envio_sin_comprobar_no_se_repite_pero_se_avisa(tmp_path, monkeypatch):
    """Pasó en la prueba real: con la vista previa del archivo aún abierta tras
    el Enter, Nova lo contaba como enviado y no decía nada."""
    import asyncio

    from openpyxl import Workbook

    from nova.tools import messaging_tools as mt

    monkeypatch.setattr(mt, "input_enabled", lambda: True)
    ruta = tmp_path / "c.xlsx"
    wb = Workbook()
    for fila in (["Nombre", "Tel"], ["Ana", "952046749"], ["Luis", "977399868"]):
        wb.active.append(fila)
    wb.save(ruta)
    avisos = []

    async def avisar(texto, speak=False):
        avisos.append(texto)

    async def enviar(telefono, _texto):
        return ("unverified", "no pude comprobarlo") if telefono.endswith("749") else ("ok", "")

    class Buscador:
        def allows(self, _ruta):
            return True

    async def main():
        tools = {t.name: t for t in mt.build(
            None, searcher=Buscador(), data_dir=tmp_path, notify=avisar, sender=enviar,
            settings={"country_code": "51", "gap_s": [0, 0], "warn_s": 0})}
        await tools["whatsapp_batch"].handler(path=str(ruta), phone_column="Tel", template="Hola {Nombre}")
        await asyncio.wait_for(asyncio.gather(*[t for t in asyncio.all_tasks()
                                                if t.get_name() == "whatsapp-batch"]), 5)
        estado = await tools["whatsapp_batch_status"].handler()
        otra_vez = await tools["whatsapp_batch"].handler(path=str(ruta), phone_column="Tel",
                                                         template="Hola {Nombre}")
        return estado, otra_vez

    estado, otra_vez = asyncio.run(main())
    assert "2 enviados de 2" in estado.summary and "1 sin poder comprobar" in estado.summary
    assert "+51 952 046 749" in avisos[-1] or "952" in avisos[-1]
    assert "míralo en WhatsApp" in avisos[-1]
    assert not otra_vez.ok                      # no lo repite: podría duplicarlo
