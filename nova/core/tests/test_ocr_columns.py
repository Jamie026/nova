"""La lectura de pantalla agrupa por columnas: la lista de chats de Discord es la de la izquierda."""
import time

from nova.capture.ocr import ScreenLine, ScreenReading


def _line(i, text, x, y):
    return ScreenLine(index=i, text=text, x=x, y=y, width=100, height=16)


def test_discord_queda_en_dos_columnas_y_el_primer_chat_es_el_de_la_izquierda():
    lines = [
        _line(1, "Mensajes directos", 112, 349), _line(2, "l'mRyan", 167, 394),
        _line(3, "Kiawa.", 167, 504), _line(4, "Jamie Nuñez", 535, 148),
        _line(5, "Ya mano", 465, 180), _line(6, "l'mRyan", 501, 64),
    ]
    reading = ScreenReading(lines=lines, origin=(0, 0), size=(1920, 1020), title="Discord", taken_at=time.monotonic())
    cols = reading.columns(lines)
    assert [l.text for l in cols[0]] == ["Mensajes directos", "l'mRyan", "Kiawa."]
    assert [l.text for l in cols[1]] == ["l'mRyan", "Jamie Nuñez", "Ya mano"]
    texto = reading.render()
    assert texto.index("— columna 1") < texto.index("2. 167,394 «l'mRyan»") < texto.index("— columna 2")
    assert "agrupadas por columnas" in texto


def test_sin_lineas_no_hay_columnas():
    reading = ScreenReading(lines=[], origin=(0, 0), size=(10, 10), title="x", taken_at=0.0)
    assert reading.render() == "No se leyó ningún texto."
