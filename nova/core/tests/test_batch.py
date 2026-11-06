"""Excel, teléfonos y envío de WhatsApp en lote por plazos."""
import asyncio

import pytest

from nova.files.tables import find_column, read_table
from nova.tools import batch, messaging_tools as mt
from nova.tools.document_tools import build as build_documents


def excel(tmp_path, filas, nombre="clientes.xlsx"):
    from openpyxl import Workbook

    libro = Workbook()
    hoja = libro.active
    hoja.title = "Clientes"
    for fila in filas:
        hoja.append(fila)
    ruta = tmp_path / nombre
    libro.save(ruta)
    return ruta


FILAS = [["Nombre", "Teléfono", "Monto"],
         ["Ana", 987654321.0, 120],
         ["Luis", "+51 912 345 678", 80.5],
         ["Sin tel", None, 10],
         ["Ana otra vez", "987 654 321", 5],
         ["Eva", "123", 1]]


# ------------------------------------------------------------------- tablas
def test_lee_excel_con_telefonos_numericos(tmp_path):
    tabla = read_table(excel(tmp_path, FILAS))
    assert tabla.columns == ["Nombre", "Teléfono", "Monto"] and tabla.sheet == "Clientes"
    assert tabla.rows[0]["Teléfono"] == "987654321"          # sin «.0»
    assert tabla.first_row == 2 and len(tabla.rows) == 5


def test_lee_csv_con_punto_y_coma_de_excel_en_espanol(tmp_path):
    ruta = tmp_path / "lista.csv"
    ruta.write_bytes("Nombre;Teléfono\nAna;987654321\n".encode("cp1252"))
    tabla = read_table(ruta)
    assert tabla.rows == [{"Nombre": "Ana", "Teléfono": "987654321"}]


def test_las_columnas_se_encuentran_sin_tildes_ni_mayusculas(tmp_path):
    tabla = read_table(excel(tmp_path, FILAS))
    assert find_column(tabla, "telefono") == "Teléfono"
    assert find_column(tabla, "TELÉFONO") == "Teléfono"
    assert find_column(tabla, "correo") is None


def test_el_xls_antiguo_da_un_mensaje_claro(tmp_path):
    with pytest.raises(ValueError, match="xlsx"):
        read_table(tmp_path / "viejo.xls")


# --------------------------------------------------------------- teléfonos
@pytest.mark.parametrize("crudo, codigo, esperado", [
    ("987654321", "51", "51987654321"),
    ("+51 987 654 321", "", "51987654321"),
    ("0051987654321", "", "51987654321"),
    ("0 987-654-321", "51", "51987654321"),
    ("987654321", "", None),          # sin código no se adivina el país
    ("123", "51", None),
    ("", "51", None),
])
def test_normaliza_telefonos(crudo, codigo, esperado):
    assert batch.normalize_phone(crudo, codigo) == esperado


# ------------------------------------------------------------------- plan
def test_plan_personaliza_salta_y_no_repite(tmp_path):
    tabla = read_table(excel(tmp_path, FILAS))
    plan = batch.plan(tabla, "telefono", "Hola {nombre}, tu saldo es {Monto}", "51")
    assert [(m.row, m.phone, m.text) for m in plan.messages] == [
        (2, "51987654321", "Hola Ana, tu saldo es 120"),
        (3, "51912345678", "Hola Luis, tu saldo es 80.5"),
    ]
    motivos = dict(plan.skipped)
    assert motivos[4] == "sin teléfono" and "repetido" in motivos[5] and "no válido" in motivos[6]


def test_plan_salta_lo_ya_enviado(tmp_path):
    tabla = read_table(excel(tmp_path, FILAS))
    plan = batch.plan(tabla, "Teléfono", "Hola {Nombre}", "51", sent={"51987654321"})
    assert [m.phone for m in plan.messages] == ["51912345678"] and plan.already_sent == 1


def test_un_campo_que_no_es_columna_se_rechaza(tmp_path):
    tabla = read_table(excel(tmp_path, FILAS))
    with pytest.raises(ValueError, match="Apellido"):
        batch.plan(tabla, "Teléfono", "Hola {Apellido}", "51")


def test_el_progreso_sobrevive_a_cerrar_nova(tmp_path):
    p = batch.Progress(tmp_path, str(tmp_path / "a.xlsx"), "Teléfono", "Hola {Nombre}")
    p.mark("51987654321", True)
    again = batch.Progress(tmp_path, str(tmp_path / "a.xlsx"), "telefono", "Hola {Nombre}")
    assert again.sent == {"51987654321"} and len(again.sent_last_day()) == 1
    otro = batch.Progress(tmp_path, str(tmp_path / "a.xlsx"), "Teléfono", "Otro mensaje")
    assert otro.sent == set()


# ------------------------------------------------------------------- lote
class Buscador:
    def allows(self, _ruta):
        return True


def herramientas(tmp_path, envios, ajustes=None, ocupado=lambda: False):
    avisos = []

    async def avisar(texto, speak=False):
        avisos.append(texto)

    async def enviar(telefono, texto):
        envios.append((telefono, texto))
        return ("invalid", "sin WhatsApp") if telefono.endswith("9") and len(envios) == 99 else ("ok", "")

    tools = {t.name: t for t in mt.build(
        None, searcher=Buscador(), data_dir=tmp_path, notify=avisar, sender=enviar,
        is_idle=lambda: not ocupado(),
        settings={"country_code": "51", "gap_s": [0, 0], "warn_s": 0, "batch_pause_min": 0,
                  **(ajustes or {})})}
    return tools, avisos


@pytest.fixture(autouse=True)
def _con_teclado(monkeypatch):
    monkeypatch.setattr(mt, "input_enabled", lambda: True)


def correr(coro):
    return asyncio.run(coro)


def test_pregunta_una_vez_con_resumen(tmp_path):
    tools, _ = herramientas(tmp_path, [])
    tool = tools["whatsapp_batch"]
    args = {"path": str(excel(tmp_path, FILAS)), "phone_column": "Teléfono",
            "template": "Hola {Nombre}"}
    pregunta = tool.confirmation_text(args)
    assert pregunta.startswith("¿Envío 2 WhatsApp desde «clientes.xlsx»?")
    assert "+51987654321: «Hola Ana»" in pregunta and "3 filas se saltan" in pregunta
    assert tool.always_confirm is True


def test_envia_por_plazos_y_no_repite_al_volver_a_pedirlo(tmp_path):
    filas = [["Nombre", "Tel"]] + [[f"P{i}", f"9000000{i:02d}"] for i in range(5)]
    ruta = str(excel(tmp_path, filas))
    envios = []

    async def main():
        tools, avisos = herramientas(tmp_path, envios, {"batch_size": 2})
        r = await tools["whatsapp_batch"].handler(path=ruta, phone_column="Tel", template="Hola {Nombre}")
        assert r.ok
        await asyncio.wait_for(asyncio.gather(*[t for t in asyncio.all_tasks()
                                                if t.get_name() == "whatsapp-batch"]), 5)
        estado = await tools["whatsapp_batch_status"].handler()
        again = await tools["whatsapp_batch"].handler(path=ruta, phone_column="Tel", template="Hola {Nombre}")
        return avisos, estado, again

    avisos, estado, again = correr(main())
    assert [t for t, _ in envios] == [f"519000000{i:02d}" for i in range(5)]
    assert sum("no toques el ratón" in a for a in avisos) == 3                  # 2 + 2 + 1
    assert "terminado" in estado.summary and "5 enviados de 5" in estado.summary
    assert not again.ok and "5 ya estaban enviados" in again.summary


def test_respeta_el_tope_diario(tmp_path):
    filas = [["Tel"]] + [[f"90000000{i}"] for i in range(3)]
    envios = []

    async def main():
        tools, avisos = herramientas(tmp_path, envios, {"daily_limit": 2})
        await tools["whatsapp_batch"].handler(path=str(excel(tmp_path, filas)), phone_column="Tel",
                                              template="Hola")
        await asyncio.sleep(0.3)
        await tools["whatsapp_batch_cancel"].handler()
        return avisos

    avisos = correr(main())
    assert len(envios) == 2
    assert any("Ya mandé 2 mensajes en las últimas 24 horas" in a for a in avisos)


def test_no_usa_el_raton_mientras_nova_esta_ocupada(tmp_path, monkeypatch):
    filas = [["Tel"], ["900000001"]]
    envios = []
    ocupada = {"si": True}

    async def main():
        tools, _ = herramientas(tmp_path, envios, ocupado=lambda: ocupada["si"])
        await tools["whatsapp_batch"].handler(path=str(excel(tmp_path, filas)), phone_column="Tel",
                                              template="Hola")
        await asyncio.sleep(0.2)
        antes = len(envios)
        await tools["whatsapp_batch_cancel"].handler()
        return antes

    assert correr(main()) == 0


def test_tres_errores_seguidos_paran_el_envio(tmp_path):
    filas = [["Tel"]] + [[f"90000000{i}"] for i in range(6)]
    avisos = []

    async def main():
        async def avisar(texto, speak=False):
            avisos.append(texto)

        async def falla(_t, _x):
            return "error", "no veo WhatsApp"

        tools = {t.name: t for t in mt.build(
            None, searcher=Buscador(), data_dir=tmp_path, notify=avisar, sender=falla,
            settings={"country_code": "51", "gap_s": [0, 0], "warn_s": 0})}
        await tools["whatsapp_batch"].handler(path=str(excel(tmp_path, filas)), phone_column="Tel",
                                              template="Hola")
        await asyncio.sleep(0.3)
        return await tools["whatsapp_batch_status"].handler()

    estado = correr(main())
    assert "detenido por errores" in estado.summary and "3 fallidos" in estado.summary
    assert any("Tuve que parar el envío" in a for a in avisos)


def test_sin_codigo_de_pais_lo_pide(tmp_path):
    tools = {t.name: t for t in mt.build(None, searcher=Buscador(), data_dir=tmp_path,
                                         settings={"country_code": ""})}
    r = correr(tools["whatsapp_batch"].handler(path=str(excel(tmp_path, FILAS[:2])),
                                               phone_column="Teléfono", template="Hola"))
    assert not r.ok and "country_code" in r.summary


# -------------------------------------------------------------- documentos
def test_read_spreadsheet_resume_columnas_y_filas(tmp_path):
    tool = {t.name: t for t in build_documents(Buscador())}["read_spreadsheet"]
    r = correr(tool.handler(path=str(excel(tmp_path, FILAS))))
    assert r.ok and "5 filas" in r.summary and "Nombre, Teléfono, Monto" in r.summary
    assert "2: Ana | 987654321 | 120" in r.summary


@pytest.mark.parametrize("crudo", ["987654321", "987 654 321", "+51 987 654 321", "+51987654321",
                                   "0051987654321", "0051 987 654 321", "51987654321", "987-654-321"])
def test_todas_las_formas_de_un_movil_peruano_son_el_mismo(crudo):
    from nova.config import DEFAULTS

    assert batch.normalize_phone(crudo, DEFAULTS["messaging"]["country_code"]) == "51987654321"


def test_marca_lo_leido_con_poca_seguridad():
    from nova.tools.document_tools import flag_lines

    texto, dudosas = flag_lines(["Destino: Ana", "Monto: S/ 1,250.80", "Op: 00451287"], [0.99, 0.91, 0.98])
    assert dudosas == 1
    assert texto.splitlines() == ["Destino: Ana", "⚠ Monto: S/ 1,250.80", "Op: 00451287"]


@pytest.mark.parametrize("leido", ["Monto: S/ 5,2e7.28", "Monto: 6,771.S3", "Monto: S/ 1.276.@9",
                                   "Monto: s/ 6, le7.ee", "Monto: sn,i39.76"])
def test_una_letra_entre_cifras_es_dudosa_aunque_el_ocr_este_seguro(leido):
    from nova.tools.document_tools import flag_lines

    assert flag_lines([leido], [0.99])[1] == 1


@pytest.mark.parametrize("limpio", ["Monto: S/ 1,250.80", "Monto: S/1,250.80", "Total: US$ 99.90",
                                    "Fecha: 05/11/2026 17:42", "Nro. de operación: 00451287",
                                    "FACTURA ELECTRÓNICA F001-000101", "Cliente: CL-0042", "BOLETA B002-9"])
def test_lo_limpio_no_se_marca(limpio):
    from nova.tools.document_tools import flag_lines

    assert flag_lines([limpio], [0.99])[1] == 0


def test_lee_un_comprobante_de_verdad_con_rapidocr(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from nova.tools.document_tools import build as build_docs

    imagen = Image.new("RGB", (560, 260), "white")
    dibujo = ImageDraw.Draw(imagen)
    fuente = ImageFont.truetype("arial.ttf", 22)
    for i, linea in enumerate(["Constancia de pago", "Destino: Rocío Ñañez", "Monto: S/ 1,250.80",
                               "Nro. de operación: 00451287"]):
        dibujo.text((20, 20 + i * 50), linea, fill="black", font=fuente)
    ruta = tmp_path / "recibo.png"
    imagen.save(ruta)
    tool = {t.name: t for t in build_docs(Buscador())}["read_image"]
    r = correr(tool.handler(path=str(ruta)))
    assert r.ok and r.data["doubtful"] <= 1      # a veces marca una de más: es lo seguro
    assert "1,250.80" in r.summary and "00451287" in r.summary and "Ñañez" in r.summary
