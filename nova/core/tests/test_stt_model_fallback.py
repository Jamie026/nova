"""Un modelo sin descargar no puede dejar a Nova sorda mientras baja 3 GB."""
from nova.stt.faster_whisper_stt import pick_available


def test_usa_el_pedido_si_esta_en_disco():
    assert pick_available("large-v3", lambda n: True) == "large-v3"


def test_sin_el_pedido_arranca_con_el_mayor_descargado():
    en_disco = {"medium", "tiny"}
    assert pick_available("large-v3", en_disco.__contains__) == "medium"


def test_primera_instalacion_descarga_el_pedido():
    assert pick_available("small", lambda n: False) == "small"


def test_large_v3_es_el_predeterminado_y_lo_baja_el_setup():
    from nova.config import DEFAULTS

    assert DEFAULTS["stt"]["model"] == "large-v3"
