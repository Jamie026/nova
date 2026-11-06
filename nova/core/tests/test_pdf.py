"""Facturas y listas de clientes en PDF: con texto, escaneadas y en tablas."""
import pytest

from nova.files.tables import read_table
from nova.tools.document_tools import read_document


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(path, pages, size=(595, 842)):
    """PDF mínimo con texto de verdad. `pages`: por página, una lista de
    («texto», x, y) y de líneas («-», x1, y1, x2, y2)."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", None,
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    kids = []
    for items in pages:
        stream = b""
        for item in items:
            if item[0] == "-":
                _, x1, y1, x2, y2 = item
                stream += f"0.5 w {x1} {y1} m {x2} {y2} l S\n".encode()
            else:
                text, x, y = item
                stream += b"BT /F1 10 Tf " + f"{x} {y} Td (".encode() + _escape(text) + b") Tj ET\n"
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream")
        content = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {size[0]} {size[1]}] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>".encode())
        kids.append(len(objects))
    objects[1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                  f"/Count {len(kids)} >>").encode()
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)
    return path


def table_page(rows, ruled=True, top=800, widths=(170, 170, 110)):
    items = []
    xs = [40]
    for w in widths:
        xs.append(xs[-1] + w)
    for i, row in enumerate(rows):
        y = top - i * 22
        for j, cell in enumerate(row):
            items.append((cell, xs[j] + 5, y - 15))
        if ruled:
            items.append(("-", xs[0], y, xs[-1], y))
    if ruled:
        bottom = top - len(rows) * 22
        items.append(("-", xs[0], bottom, xs[-1], bottom))
        for x in xs:
            items.append(("-", x, top, x, bottom))
    return items


FACTURA = ["DISTRIBUIDORA EL SOL E.I.R.L.", "RUC 20601234567", "FACTURA ELECTRÓNICA F001-000123",
           "SEÑOR(ES): TORRES VEGA ANA MARÍA", "Código de cliente: CL-0042",
           "OP. GRAVADA S/ 1,060.00", "IGV 18% S/ 190.80", "IMPORTE TOTAL S/ 1,250.80"]


def test_factura_pdf_con_texto_se_lee_exacta_y_sin_ocr(tmp_path):
    pdf = make_pdf(tmp_path / "f.pdf", [[(t, 50, 780 - i * 20) for i, t in enumerate(FACTURA)]])
    lines, scores = read_document(pdf)
    assert lines == FACTURA                         # tildes y Ñ incluidas
    assert set(scores) == {1.0}                     # texto del PDF: nada dudoso


def test_factura_pdf_escaneada_pasa_por_el_ocr(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    imagen = Image.new("RGB", (1240, 900), "white")
    dibujo = ImageDraw.Draw(imagen)
    fuente = ImageFont.truetype("arial.ttf", 30)
    for i, linea in enumerate(FACTURA):
        dibujo.text((60, 60 + i * 90), linea, fill="black", font=fuente)
    pdf = tmp_path / "escaneada.pdf"
    imagen.save(pdf, "PDF", resolution=150)
    lines, scores = read_document(pdf)
    assert any("1,250.80" in l for l in lines) and any("CL-0042" in l for l in lines)
    assert all(s < 1.0 for s in scores)             # vino del OCR, con su confianza


CLIENTES = [["Nombre completo", "Celular", "ID Cliente"],
            ["Ana María Torres", "987654321", "CL-0042"],
            ["Luis Quispe Mamani", "912345678", "CL-0043"],
            ["Rocío Ñañez", "955444333", "CL-0044"]]


@pytest.mark.parametrize("con_lineas", [True, False])
def test_lista_de_clientes_en_pdf(tmp_path, con_lineas):
    pdf = make_pdf(tmp_path / "clientes.pdf", [table_page(CLIENTES, ruled=con_lineas)])
    tabla = read_table(pdf)
    assert tabla.columns == CLIENTES[0]
    assert [list(r.values()) for r in tabla.rows] == CLIENTES[1:]


def test_lista_en_varias_paginas_con_la_cabecera_repetida(tmp_path):
    pdf = make_pdf(tmp_path / "clientes.pdf", [table_page(CLIENTES[:3]), table_page([CLIENTES[0], CLIENTES[3]])])
    tabla = read_table(pdf)
    assert [r["ID Cliente"] for r in tabla.rows] == ["CL-0042", "CL-0043", "CL-0044"]


def test_una_lista_escaneada_se_rechaza_con_un_motivo_claro(tmp_path):
    from PIL import Image

    pdf = tmp_path / "foto.pdf"
    Image.new("RGB", (600, 800), "white").save(pdf, "PDF")
    with pytest.raises(ValueError, match="escaneado"):
        read_table(pdf)


def test_lista_sin_lineas_en_varias_paginas_sin_repetir_cabecera(tmp_path):
    pdf = make_pdf(tmp_path / "clientes.pdf", [table_page(CLIENTES[:3], ruled=False),
                                               table_page(CLIENTES[3:], ruled=False)])
    tabla = read_table(pdf)
    assert tabla.columns == CLIENTES[0]
    assert [r["Celular"] for r in tabla.rows] == ["987654321", "912345678", "955444333"]
