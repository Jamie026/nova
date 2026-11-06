"""Facturas → cliente del Excel → monto por WhatsApp: qué se envía solo y qué no."""
import asyncio
from types import SimpleNamespace

import pytest

from nova.files.tables import Table
from nova.tools import invoices as inv
from nova.tools import messaging_tools as mt
from nova.tools.registry import ToolRegistry


def tabla(columnas, filas):
    return Table(columnas, [dict(zip(columnas, f)) for f in filas])


CLIENTES = tabla(["Full Name", "E-mail", "Celular", "DNI"], [
    ["Ana María Torres", "ana@gmail.com", "987654321", "45678912"],
    ["Luis Quispe Mamani", "luisq@hotmail.com", "+51 912 345 678", "10203040"],
    ["José Huamán", "", "0051 955 444 333", ""],
    ["Ana Lucía Ramos", "aramos@gmail.com", "966111222", ""],
])


# ------------------------------------------------------------- columnas
@pytest.mark.parametrize("cabeceras", [
    ["Nombre completo", "Correo electrónico", "Teléfono", "RUC"],
    ["name", "email", "phone", "documento"],
    ["Cliente", "Mail", "WhatsApp", "Nro documento"],
    ["NOMBRES Y APELLIDOS", "CORREO", "CELULAR", "DNI"],
])
def test_reconoce_las_columnas_por_su_titulo(cabeceras):
    c = inv.detect_columns(tabla(cabeceras, [["Ana Torres", "a@b.com", "987654321", "123"]]))
    assert (c.name, c.email, c.phone, c.document) == tuple(cabeceras)


def test_sin_titulos_reconocibles_mira_el_contenido():
    t = tabla(["A", "B", "C"], [["Ana Torres", "ana@x.com", "987 654 321"],
                                ["Luis Quispe", "luis@x.com", "+51912345678"]])
    c = inv.detect_columns(t)
    assert (c.name, c.email, c.phone) == ("A", "B", "C")


# --------------------------------------------------------------- cliente
def test_por_documento_y_por_correo_es_exacto():
    c = inv.detect_columns(CLIENTES)
    assert inv.match_client(CLIENTES, c, "", "", "45678912").row["Full Name"] == "Ana María Torres"
    assert inv.match_client(CLIENTES, c, "", "LUISQ@hotmail.com", "").row["Full Name"] == "Luis Quispe Mamani"


@pytest.mark.parametrize("en_factura, esperado", [
    ("TORRES ANA MARIA", "Ana María Torres"),       # apellido primero y en mayúsculas
    ("Quispe Mamani, Luis", "Luis Quispe Mamani"),
    ("JOSE HUAMAN", "José Huamán"),
])
def test_por_nombre_sin_importar_orden_ni_tildes(en_factura, esperado):
    m = inv.match_client(CLIENTES, inv.detect_columns(CLIENTES), en_factura, "", "")
    assert m.row is not None and m.row["Full Name"] == esperado


def test_un_nombre_ambiguo_o_parecido_no_se_da_por_bueno():
    c = inv.detect_columns(CLIENTES)
    assert inv.match_client(CLIENTES, c, "Ana", "", "").row is None                # dos Anas
    # «Ana Torres Vega» no es «Ana María Torres»: falta una palabra en cada lado.
    assert inv.match_client(CLIENTES, c, "Ana Torres Vega", "", "").row is None
    m = inv.match_client(CLIENTES, c, "Ana Ramírez", "", "")
    assert m.row is None and "no lo bastante" in m.reason
    assert "no encontré" in inv.match_client(CLIENTES, c, "Pedro Castillo", "", "").reason


# ----------------------------------------------------------------- monto
def test_el_monto_tiene_que_estar_en_una_linea_segura():
    lineas = ["FACTURA F001-123", "OP. GRAVADA 1,060.00", "IGV 190.80", "IMPORTE TOTAL S/ 1,250.80"]
    assert inv.verify_amount("1,250.80", lineas) == (True, "")
    assert inv.verify_amount("1250.80", lineas)[0]                          # mismo número
    assert not inv.verify_amount("1,258.80", lineas)[0]                     # inventado
    ok, motivo = inv.verify_amount("1,250.80", lineas[:3] + ["⚠ IMPORTE TOTAL S/ 1,250.80"])
    assert not ok and "poca seguridad" in motivo
    assert not inv.verify_amount("", lineas)[0]


def test_el_mensaje_se_arma_y_avisa_de_campos_que_no_existen():
    assert inv.template_problems("Hola {Nombre}, debes {Moneda} {Monto} ({Factura}) {Celular}", CLIENTES) == []
    assert inv.template_problems("Hola {Apodo}", CLIENTES) == ["Apodo"]
    assert inv.render("Hola {nombre}, debes {MONEDA} {Monto}",
                      {"Nombre": "Ana", "Moneda": "S/", "Monto": "1,250.80"}) == "Hola Ana, debes S/ 1,250.80"


def test_extraccion_tolera_texto_alrededor_del_json():
    data = inv.parse_extraction('Aquí está:\n```json\n{"cliente": "Ana", "monto": "12.50"}\n```')
    assert data == {"cliente": "Ana", "monto": "12.50"}
    assert inv.parse_extraction("no sé") == {}


# --------------------------------------------------------- todo el flujo
FACTURAS = {
    b"f1": (["FACTURA F001-1", "Cliente: TORRES ANA MARIA", "TOTAL A PAGAR S/ 1,250.80"],
            {"cliente": "TORRES ANA MARIA", "monto": "1,250.80", "moneda": "S/", "factura": "F001-1"}),
    b"f2": (["BOLETA B002-9", "Sr. Luis Quispe Mamani", "TOTAL S/ 80.50"],
            {"cliente": "Luis Quispe Mamani", "monto": "80.50", "moneda": "S/", "factura": "B002-9"}),
    b"f3": (["FACTURA F001-2", "Cliente: JOSE HUAMAN", "TOTAL S/ 300.00"],          # monto leído con duda
            {"cliente": "JOSE HUAMAN", "monto": "300.00", "moneda": "S/", "factura": "F001-2"}),
    b"f4": (["FACTURA F001-3", "Cliente: PEDRO CASTILLO", "TOTAL S/ 99.00"],        # no está en el Excel
            {"cliente": "PEDRO CASTILLO", "monto": "99.00", "moneda": "S/", "factura": "F001-3"}),
}
DUDOSAS = {b"f3": "TOTAL S/ 300.00"}


def preparar(tmp_path):
    from openpyxl import Workbook

    carpeta = tmp_path / "facturas"
    carpeta.mkdir()
    for clave in FACTURAS:
        (carpeta / f"{clave.decode()}.png").write_bytes(clave)
    libro = Workbook()
    libro.active.append(CLIENTES.columns)
    for fila in CLIENTES.rows:
        libro.active.append([fila[c] for c in CLIENTES.columns])
    excel = tmp_path / "clientes.xlsx"
    libro.save(excel)
    return carpeta, excel


def herramientas(tmp_path, enviados, avisos):
    def leer(ruta):
        datos = ruta.read_bytes()
        lineas = FACTURAS[datos][0]
        return lineas, [0.80 if l == DUDOSAS.get(datos) else 0.99 for l in lineas]

    async def modelo(mensajes):
        clave = next(k for k, (lineas, _) in FACTURAS.items() if mensajes[-1]["content"] == "\n".join(lineas))
        import json
        return SimpleNamespace(text=json.dumps(FACTURAS[clave][1]))

    async def enviar(telefono, texto):
        enviados.append((telefono, texto))
        return "ok", ""

    async def avisar(texto, speak=False):
        avisos.append(texto)

    buscador = SimpleNamespace(allows=lambda _p: True)
    return {t.name: t for t in mt.build(
        None, searcher=buscador, data_dir=tmp_path, notify=avisar, sender=enviar, llm_chat=modelo,
        read_text=leer, settings={"country_code": "51", "gap_s": [0, 0], "warn_s": 0, "batch_pause_min": 0})}


@pytest.fixture(autouse=True)
def _teclado(monkeypatch):
    monkeypatch.setattr(mt, "input_enabled", lambda: True)


async def esperar_lote():
    await asyncio.wait_for(asyncio.gather(*[t for t in asyncio.all_tasks()
                                            if t.get_name() == "whatsapp-batch"]), 5)


def test_envia_lo_seguro_sin_preguntar_y_al_final_dice_lo_que_falta(tmp_path):
    from openpyxl import load_workbook

    carpeta, excel = preparar(tmp_path)
    enviados, avisos = [], []

    async def main():
        tools = herramientas(tmp_path, enviados, avisos)
        r = await tools["process_invoices"].handler(path=str(carpeta), clients=str(excel),
                                                     template="Hola {Nombre}, tu factura {Factura} es de {Moneda} {Monto}")
        assert r.ok and "Celular" in r.summary and "Full Name" in r.summary
        await esperar_lote()
        return tools

    tools = asyncio.run(main())
    assert enviados == [
        ("51987654321", "Hola Ana María Torres, tu factura F001-1 es de S/ 1,250.80"),
        ("51912345678", "Hola Luis Quispe Mamani, tu factura B002-9 es de S/ 80.50"),
    ]
    final = avisos[-1]
    assert final.startswith("Mandé 2 de las 4 facturas. Me faltaron 2, una porque no estaba seguro del monto")
    reporte = next(carpeta.glob("Nova - facturas *.xlsx"))
    libro = load_workbook(reporte)
    pendientes = {f[0]: f[-1] for f in libro["Pendientes"].iter_rows(min_row=2, values_only=True)}
    assert "poca seguridad" in pendientes["f3.png"] and "no encontré" in pendientes["f4.png"]
    assert libro["Enviadas"].max_row == 3
    # Sin preguntar por defecto, como pidió el usuario.
    from nova.config import DEFAULTS
    registry = ToolRegistry(risk_policy=DEFAULTS["security"], auto_approve=DEFAULTS["security"]["auto_approve"])
    assert not registry.needs_confirmation(tools["process_invoices"], {})


def test_las_dudosas_se_envian_solo_tras_un_si_y_sin_repetir_las_otras(tmp_path):
    carpeta, excel = preparar(tmp_path)
    enviados, avisos = [], []

    async def main():
        tools = herramientas(tmp_path, enviados, avisos)
        await tools["process_invoices"].handler(path=str(carpeta), clients=str(excel),
                                                template="{Nombre}: {Monto}")
        await esperar_lote()
        pregunta = tools["invoices_send_doubtful"].confirmation_text({})
        r = await tools["invoices_send_doubtful"].handler()
        await esperar_lote()
        await tools["process_invoices"].handler(path=str(carpeta), clients=str(excel),
                                                           template="{Nombre}: {Monto}")
        await esperar_lote()
        return tools, pregunta, r

    tools, pregunta, r = asyncio.run(main())
    assert tools["invoices_send_doubtful"].always_confirm is True
    assert pregunta == ("Tengo esa factura con el monto dudoso: José Huamán por S/ 300.00. "
                        "¿Las mando tal como las leí?")
    assert r.ok
    # Ana, Luis, José y nada más: repetir la orden no duplica.
    assert [t for t, _ in enviados] == ["51987654321", "51912345678", "51955444333"]


def test_un_campo_del_mensaje_que_no_existe_se_dice_antes_de_empezar(tmp_path):
    carpeta, excel = preparar(tmp_path)
    r = asyncio.run(herramientas(tmp_path, [], [])["process_invoices"].handler(
        path=str(carpeta), clients=str(excel), template="Hola {Apodo}"))
    assert not r.ok and "{Apodo}" in r.summary


# ------------------------------------------------- por un dato que dice el usuario
CODIGOS = tabla(["Contacto", "Cod. Cliente", "Celular"], [
    ["Ana María Torres", "CL-0042", "987654321"],
    ["Luis Quispe", "CL-0043", "912345678"],
    ["Luis Quispe", "CL-0108", "955444333"],          # mismo nombre, otro cliente
])


def test_busca_por_el_dato_en_su_columna_o_en_cualquiera():
    m, col = inv.match_by_key(CODIGOS, "Cod. Cliente", "cl 0043", "código de cliente")
    assert m.row["Celular"] == "912345678" and col == "Cod. Cliente"
    m, col = inv.match_by_key(CODIGOS, None, "CL-0108", "código de cliente")     # columna sin decir
    assert m.row["Celular"] == "955444333" and col == "Cod. Cliente"
    m, _ = inv.match_by_key(CODIGOS, None, "CL-9999", "código de cliente")
    assert m.row is None and "no encontré código de cliente «CL-9999»" in m.reason


def test_el_dato_tiene_que_leerse_sin_duda():
    lineas = ["FACTURA F001-1", "Código de cliente: CL-0043", "TOTAL S/ 80.50"]
    assert inv.verify_value("CL-0043", lineas) == (True, "")
    assert not inv.verify_value("CL-0048", lineas)[0]                           # no está escrito así
    ok, motivo = inv.verify_value("CL-0043", ["⚠ Código de cliente: CL-0043"])
    assert not ok and "poca seguridad" in motivo


def test_el_prompt_pide_el_dato_del_usuario():
    assert "«N° de suministro»" in inv.extraction_prompt("N° de suministro")
    assert "dato_clave" not in inv.extraction_prompt("")



def test_el_codigo_se_encuentra_aunque_la_factura_no_lo_llame_asi(tmp_path, monkeypatch):
    """Pasó de verdad: «guíate del código de factura», y la factura sólo dice
    «FACTURA ELECTRÓNICA F001-000101». El modelo devolvió el dato vacío."""
    from openpyxl import Workbook

    carpeta = tmp_path / "facturas"
    carpeta.mkdir()
    (carpeta / "f1.png").write_bytes(b"f1")
    libro = Workbook()
    libro.active.append(["Nombre", "Teléfono", "Código factura"])
    libro.active.append(["Ana Torres Vega", "952046749", "F001-000101"])
    excel = tmp_path / "clientes.xlsx"
    libro.save(excel)
    enviados, avisos = [], []

    def leer(_ruta):
        return ["FACTURA ELECTRÓNICA F001-000101", "SEÑOR(ES): ANA TORRES VEGA", "IMPORTE TOTAL S/ 350.00"], [1.0] * 3

    async def modelo(_m):
        import json
        return SimpleNamespace(text=json.dumps({"cliente": "ANA TORRES VEGA", "monto": "350.00", "moneda": "S/",
                                                "factura": "F001-000101", "dato_clave": ""}))

    async def enviar(telefono, texto):
        enviados.append((telefono, texto)); return "ok", ""

    async def avisar(t, speak=False):
        avisos.append(t)

    tools = {t.name: t for t in mt.build(
        None, searcher=SimpleNamespace(allows=lambda _p: True), data_dir=tmp_path, notify=avisar,
        sender=enviar, llm_chat=modelo, read_text=leer,
        settings={"country_code": "51", "gap_s": [0, 0], "warn_s": 0, "batch_pause_min": 0})}

    async def main():
        await tools["process_invoices"].handler(path=str(carpeta), clients=str(excel), template="{Monto}",
                                                key_field="código de factura", key_column="Código factura")
        await esperar_lote()

    asyncio.run(main())
    assert enviados == [("51952046749", "350.00")]
