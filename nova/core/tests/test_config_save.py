"""Guardar ajustes no puede congelar los valores por defecto.

El fallo real: el panel guardaba una foto entera de la configuración, y como
settings.json pisa a DEFAULTS, cada mejora posterior de un valor por defecto se
ignoraba para siempre. vad.end_ms subió de 450 a 800 en el código y el usuario
seguía con 450 sin saberlo.
"""
import json

import pytest

from nova import config as cfgmod
from nova.config import DEFAULTS, Config


@pytest.fixture
def archivo(tmp_path, monkeypatch):
    ruta = tmp_path / "settings.json"
    monkeypatch.setattr(cfgmod, "settings_file", lambda: ruta)
    return ruta


def test_sin_cambios_no_guarda_nada(archivo):
    Config(Config.load().as_dict()).save()
    assert json.loads(archivo.read_text("utf-8")) == {}


def test_solo_guarda_lo_que_el_usuario_toco(archivo):
    cfg = Config(Config.load().as_dict())
    cfg.patch({"vad": {"end_ms": 1200}, "avatar": {"scale": 1.4}})
    cfg.save()
    assert json.loads(archivo.read_text("utf-8")) == {
        "vad": {"end_ms": 1200}, "avatar": {"scale": 1.4}}


def test_un_default_nuevo_llega_aunque_haya_settings_guardados(archivo, monkeypatch):
    cfg = Config(Config.load().as_dict())
    cfg.patch({"avatar": {"scale": 1.4}})
    cfg.save()
    # El código cambia un valor por defecto después de que el usuario guardara.
    monkeypatch.setitem(DEFAULTS["vad"], "end_ms", 999)
    assert Config.load().get("vad.end_ms") == 999
    assert Config.load().get("avatar.scale") == 1.4


def test_lo_que_viene_del_entorno_no_se_persiste(archivo, monkeypatch):
    monkeypatch.setenv("NOVA_TTS_PROVIDER", "xtts")
    cfg = Config.load()
    assert cfg.get("tts.provider") == "xtts"
    cfg.save()
    guardado = json.loads(archivo.read_text("utf-8"))
    assert "tts" not in guardado     # y sin dejar {"tts": {}} de rastro
