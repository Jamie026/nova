"""Que Nova no escriba como una máquina."""
import pytest

from nova.agent.tone import naturalize


@pytest.mark.parametrize("modelo, natural", [
    ("Listo — ya abrí Spotify.", "Listo, ya abrí Spotify."),
    ("Abrí Spotify – y puse tu lista.", "Abrí Spotify, y puse tu lista."),
    ("Hecho - lo dejé en Descargas.", "Hecho, lo dejé en Descargas."),
    ("Encontré:\n- clientes.xlsx\n- facturas.pdf", "Encontré: clientes.xlsx y facturas.pdf."),
    ("**Listo**, ya está.", "Listo, ya está."),
    ("Hice esto:\n1. Abrí Gmail.\n2. Puse el correo.\n3. Lo dejé listo.",
     "Hice esto: Abrí Gmail, puse el correo y lo dejé listo."),
])
def test_quita_los_tics_de_maquina(modelo, natural):
    assert naturalize(modelo) == natural


@pytest.mark.parametrize("intacto", [
    "Tu factura F001-000101 ya salió.",
    "Te llamo al 987-654-321 si quieres.",
    "Conecté el Wi-Fi.",
    "Te mando «Hola — nos vemos - mañana» a Ana.",       # el mensaje literal no se toca
])
def test_respeta_codigos_palabras_y_mensajes_literales(intacto):
    assert naturalize(intacto) == intacto
