"""La preparación del audio de referencia, que es lo que decide si la voz clonada
se parece o no. Todo esto es lógica pura sobre arrays: no hace falta GPU ni
modelo para probarlo."""
import numpy as np
import pytest
import soundfile as sf

from nova.tts.voice_profile import (SAMPLE_RATE, _normalize, _trim_silence,
                                    build_reference, load_clips)


def _tono(segundos: float, amplitud: float = 0.3, sr: int = SAMPLE_RATE) -> np.ndarray:
    t = np.linspace(0, segundos, int(segundos * sr), endpoint=False)
    return (amplitud * np.sin(2 * np.pi * 180 * t)).astype(np.float32)


def _escribe(carpeta, nombre: str, audio: np.ndarray, sr: int = SAMPLE_RATE):
    ruta = carpeta / nombre
    sf.write(str(ruta), audio, sr, subtype="PCM_16")
    return ruta


def test_recorta_el_silencio_de_los_extremos():
    silencio = np.zeros(SAMPLE_RATE, dtype=np.float32)
    clip = np.concatenate([silencio, _tono(1.0), silencio])
    recortado = _trim_silence(clip)
    # Queda el tono más el pequeño margen que se deja a propósito para no
    # comerse la consonante inicial ni la caída final.
    assert SAMPLE_RATE <= recortado.size < clip.size
    assert recortado.size < int(1.3 * SAMPLE_RATE)


def test_normalizar_iguala_volumen_sin_saturar():
    flojo, fuerte = _normalize(_tono(1.0, 0.02)), _normalize(_tono(1.0, 0.9))
    rms = lambda a: float(np.sqrt(np.mean(a ** 2)))
    assert rms(flojo) == pytest.approx(rms(fuerte), rel=0.05)
    assert np.max(np.abs(fuerte)) <= 0.97


def test_los_clips_largos_van_primero(tmp_path):
    # En material de videojuego los gritos son cortos y el diálogo es largo, así
    # que ordenar por duración es lo que separa uno de otro.
    _escribe(tmp_path, "grito.wav", _tono(0.6))
    _escribe(tmp_path, "dialogo.wav", _tono(4.0))
    clips = load_clips(sorted(tmp_path.glob("*.wav")))
    assert [c.path.name for c in clips] == ["dialogo.wav", "grito.wav"]


def test_descarta_los_clips_demasiado_cortos(tmp_path):
    _escribe(tmp_path, "chasquido.wav", _tono(0.2))
    assert load_clips([tmp_path / "chasquido.wav"]) == []


def test_la_referencia_respeta_el_presupuesto_y_el_orden(tmp_path):
    for nombre, dur in (("a.wav", 5.0), ("b.wav", 4.0), ("c.wav", 3.0), ("d.wav", 2.0)):
        _escribe(tmp_path, nombre, _tono(dur))
    salida = tmp_path / "out" / "reference.wav"
    elegidos = build_reference(sorted(tmp_path.glob("*.wav")), salida, target_seconds=8.0)

    assert [c.path.name for c in elegidos] == ["a.wav", "b.wav"]
    info = sf.info(str(salida))
    assert info.samplerate == SAMPLE_RATE and info.channels == 1
    assert 9.0 < info.duration < 10.0     # 5 + 4 + la respiración intermedia


def test_se_pueden_excluir_por_nombre(tmp_path):
    _escribe(tmp_path, "VO_Personaje_Attack_01.wav", _tono(4.0))
    _escribe(tmp_path, "VO_Personaje_Dialogo_01.wav", _tono(3.0))
    elegidos = build_reference(sorted(tmp_path.glob("*.wav")), tmp_path / "ref.wav",
                               exclude=["attack"])
    assert [c.path.name for c in elegidos] == ["VO_Personaje_Dialogo_01.wav"]


def test_sin_material_utilizable_falla_claro(tmp_path):
    _escribe(tmp_path, "nada.wav", np.zeros(SAMPLE_RATE, dtype=np.float32))
    with pytest.raises(ValueError):
        build_reference([tmp_path / "nada.wav"], tmp_path / "ref.wav")


def test_remuestrea_sin_cambiar_la_duracion(tmp_path):
    # Los .ogg del usuario vienen a 192 kHz y hay que bajarlos a 24 kHz.
    _escribe(tmp_path, "agudo.wav", _tono(2.0, sr=48_000), sr=48_000)
    clips = load_clips([tmp_path / "agudo.wav"])
    assert clips and clips[0].seconds == pytest.approx(2.0, abs=0.1)


# ---------------------------------------------------------------------------
# El guardia contra el atasco de XTTS. Vive en el motor, pero es aritmética pura
# y se prueba sin GPU ni modelo.

def test_detecta_el_atasco_por_duracion():
    from nova.tts.xtts_engine import looks_degenerate

    texto = "I couldn't close Discord, it's still open. Should I try again?"
    # Lo que salió de verdad: 10,6 s para 61 caracteres, repitiendo «I, I, I...».
    assert looks_degenerate(texto, 10.6)
    # Lo normal para esa frase ronda los 4 s.
    assert not looks_degenerate(texto, 4.0)
    assert not looks_degenerate(texto, 7.0)


def test_no_marca_como_atasco_una_frase_larga_normal():
    from nova.tts.xtts_engine import looks_degenerate

    texto = ("I found three images called Corin in Downloads. I opened the newest "
             "one, tell me if you want another.")
    assert not looks_degenerate(texto, 7.3)


def test_el_guardia_no_se_rompe_con_entradas_raras():
    from nova.tts.xtts_engine import looks_degenerate

    assert not looks_degenerate("", 5.0)
    assert not looks_degenerate("   ", 5.0)
    assert not looks_degenerate("hola", 0.0)
    # Una palabra suelta tiene poco margen, pero el de gracia evita el falso positivo.
    assert not looks_degenerate("Done.", 1.4)
