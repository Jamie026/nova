"""Quien clona el repo tiene la voz sin tocar el .env: proveedor «auto» y la
referencia incluida en nova/tts/voices/."""
from pathlib import Path

from nova import app as app_module
from nova.config import Config


def _config(**tts):
    config = Config(Config.load().as_dict())
    for key, value in tts.items():
        config.patch({"tts": {key: value}})
    return config


def test_por_defecto_el_proveedor_es_auto():
    assert Config.load().get("tts.provider") == "auto"


def test_auto_elige_chatterbox_si_esta_instalado(monkeypatch):
    monkeypatch.setattr(app_module, "chatterbox_installed", lambda: True)
    assert app_module.resolve_tts_provider(_config(provider="auto")) == "chatterbox"
    monkeypatch.setattr(app_module, "chatterbox_installed", lambda: False)
    assert app_module.resolve_tts_provider(_config(provider="auto")) == "edge"


def test_lo_explicito_manda_y_xtts_es_chatterbox(monkeypatch):
    monkeypatch.setattr(app_module, "chatterbox_installed", lambda: True)
    assert app_module.resolve_tts_provider(_config(provider="edge")) == "edge"
    assert app_module.resolve_tts_provider(_config(provider="xtts")) == "chatterbox"


def test_la_referencia_cae_a_la_incluida_en_el_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "app_data_dir", lambda: tmp_path, raising=False)
    import nova.paths
    monkeypatch.setattr(nova.paths, "app_data_dir", lambda: tmp_path)
    bundled = tmp_path / "repo" / "reference.wav"
    bundled.parent.mkdir()
    bundled.write_bytes(b"RIFF")
    monkeypatch.setattr(app_module, "BUNDLED_REFERENCE", bundled)
    # Sin nada configurado ni en %APPDATA%: la del repo.
    assert app_module.find_reference(_config(reference="")) == bundled
    # La del usuario en %APPDATA% gana a la del repo.
    (tmp_path / "voices").mkdir()
    mine = tmp_path / "voices" / "reference.wav"
    mine.write_bytes(b"RIFF")
    assert app_module.find_reference(_config(reference="")) == mine
    # Y la configurada gana a todas.
    other = tmp_path / "otra.wav"
    other.write_bytes(b"RIFF")
    assert app_module.find_reference(_config(reference=str(other))) == other
    # Una configurada que no existe no vale: se sigue buscando.
    assert app_module.find_reference(_config(reference=str(tmp_path / "no.wav"))) == mine


def test_el_repo_incluye_la_referencia():
    assert app_module.BUNDLED_REFERENCE == Path(app_module.__file__).parent / "tts" / "voices" / "reference.wav"
    assert app_module.BUNDLED_REFERENCE.is_file(), "falta core/nova/tts/voices/reference.wav"


def test_el_idioma_de_la_voz_sigue_al_motor_construido():
    class ChatterboxEngine:   # el nombre es lo que se mira: sin importar torch
        pass

    class EdgeTts:
        pass

    assert app_module.speak_language(_config(speak_language=""), ChatterboxEngine()) == "en"
    assert app_module.speak_language(_config(speak_language=""), EdgeTts()) == "es"
    assert app_module.speak_language(_config(speak_language="pt"), ChatterboxEngine()) == "pt"
