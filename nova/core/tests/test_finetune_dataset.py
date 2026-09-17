"""El corte por silencios de nova/tts/finetune.py (sin GPU ni Whisper)."""
import numpy as np
import pytest

pytest.importorskip("librosa")

from nova.tts import finetune


def _voz(seconds: float, sr: int = 24000) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _silencio(seconds: float, sr: int = 24000) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def test_pausa_larga_separa_frases():
    audio = np.concatenate([_voz(2.0), _silencio(1.0), _voz(1.5)])
    trozos = finetune._split_by_silence(audio)
    assert len(trozos) == 2
    d1, d2 = [(e - s) / 24000 for s, e in trozos]
    assert 2.0 <= d1 <= 2.4 and 1.5 <= d2 <= 1.9   # voz + aire, no más


def test_pausa_corta_no_separa():
    audio = np.concatenate([_voz(2.0), _silencio(0.2), _voz(1.5)])
    assert len(finetune._split_by_silence(audio)) == 1


def test_no_junta_mas_de_lo_que_admite_el_modelo():
    # Frases de 6 s con pausas de 0,2 s: juntarlas pasaría de MAX_CLIP_S.
    audio = np.concatenate([_voz(6.0), _silencio(0.2), _voz(6.0)])
    assert len(finetune._split_by_silence(audio)) == 2


def test_silencio_total_no_da_trozos():
    assert finetune._split_by_silence(_silencio(3.0)) == []
