"""Construye el audio de referencia que clona la voz.

Los modelos de clonado zero-shot no se entrenan: se les da un trozo de voz y
copian timbre, acento y entonación de *ese* trozo. Dos cosas lo estropean de
verdad, y las dos se arreglan aquí:

  * **Silencio.** Los clips vienen con cola de silencio; concatenarlos tal cual
    gasta en nada la mitad del presupuesto de referencia.
  * **Aliasing.** El material puede venir a 192 kHz y hay que bajarlo a 24 kHz.
    Decimar sin filtrar antes mete chirridos que el modelo copia como si fueran
    parte de la voz. De ahí `resample_poly` y no una interpolación a secas.

Sobre *qué* clips elegir, lo que manda es la medición, y ha hecho falta medir dos
veces porque la primera tanda mentía. Con doce muestras por configuración las
diferencias parecían claras y apuntaban a un sitio; con cuarenta y barras de
error, apuntaban al contrario. XTTS muestrea, y su varianza entre ejecuciones se
come cualquier conclusión sacada de pocas frases.

Lo que queda en pie, con 40 síntesis por configuración medidas contra su propia
transcripción:

    un solo clip largo y homogéneo (34 s de diálogo)  0,918   1 mala de 40
    seis clips cortos concatenados (30 s)             0,906   4 malas de 40
    todo mezclado (60 s de gritos + menús + diálogo)  0,737   7 malas de 12

Es decir: **un trozo largo de habla continua vale más que muchos trozos sueltos, y
mezclar material heterogéneo es peor que quedarse corto.** Por eso el presupuesto
por defecto son 30 s y no más: obliga a quedarse con lo mejor en vez de amontonar.
Con los clips ordenados de más largo a más corto, un diálogo de 34 s llena el
presupuesto él solo y los gritos de combate ni entran.

(También se probó subir `gpt_cond_len` de 6 a 30 s al condicionar el modelo, por
si con una referencia larga aprovechaba más. Empeora: 0,918 -> 0,861. Se quedó
como estaba.)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import soundfile as sf

log = logging.getLogger("nova.tts.voice")

SAMPLE_RATE = 24_000          # lo que espera Chatterbox (y XTTS, antes)
TARGET_SECONDS = 30.0         # medido: más referencia no empeora, y algo ayuda
GAP_SECONDS = 0.25            # respiración entre frases, para que no se peguen
_SILENCE_FLOOR = 0.02         # relativo al pico del clip
# Absoluto: por debajo de esto no hay voz que copiar, sólo ruido de fondo o un
# clip mudo. El umbral relativo no lo detecta, porque todo es relativo a su
# propio pico.
_MIN_PEAK = 0.005


@dataclass(slots=True)
class Clip:
    path: Path
    audio: np.ndarray
    seconds: float


def _to_mono_24k(path: Path) -> np.ndarray:
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if rate == SAMPLE_RATE:
        return mono
    from math import gcd

    from scipy.signal import resample_poly

    factor = gcd(int(rate), SAMPLE_RATE)
    return resample_poly(mono, SAMPLE_RATE // factor, int(rate) // factor).astype(np.float32)


def _trim_silence(audio: np.ndarray) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    # Un clip mudo no se "recorta a sí mismo": no queda nada. Devolver el
    # original hacía que un archivo en silencio pasara por material válido y se
    # colara en la referencia como un hueco de un segundo.
    if peak <= 0:
        return audio[:0]
    loud = np.flatnonzero(np.abs(audio) > peak * _SILENCE_FLOOR)
    if loud.size == 0:
        return audio[:0]
    # Un pelín de margen para no cortar la consonante inicial ni la caída final.
    start = max(0, int(loud[0]) - SAMPLE_RATE // 50)
    end = min(audio.size, int(loud[-1]) + SAMPLE_RATE // 25)
    return audio[start:end]


def _normalize(audio: np.ndarray, target_rms: float = 0.08) -> np.ndarray:
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    if rms <= 0:
        return audio
    scaled = audio * (target_rms / rms)
    peak = float(np.max(np.abs(scaled)))
    if peak > 0.97:                     # sin recortar: clipping = distorsión clonada
        scaled *= 0.97 / peak
    return scaled.astype(np.float32)


def load_clips(sources: Iterable[Path]) -> list[Clip]:
    clips: list[Clip] = []
    for path in sources:
        try:
            audio = _trim_silence(_to_mono_24k(path))
        except Exception as exc:
            log.warning("no pude leer %s: %s", path.name, exc)
            continue
        if audio.size < SAMPLE_RATE // 2:   # menos de 0,5 s útiles
            continue
        if float(np.max(np.abs(audio))) < _MIN_PEAK:
            log.debug("descarto %s: prácticamente mudo", path.name)
            continue
        clips.append(Clip(path, audio, audio.size / SAMPLE_RATE))
    clips.sort(key=lambda c: c.seconds, reverse=True)
    return clips


def build_reference(sources: Iterable[Path], out: Path,
                    target_seconds: float = TARGET_SECONDS,
                    exclude: Iterable[str] = ()) -> list[Clip]:
    """Escribe el WAV de referencia y devuelve los clips que entraron."""
    blocked = tuple(token.lower() for token in exclude)
    candidates = [
        clip for clip in load_clips(sources)
        if not any(token in clip.path.name.lower() for token in blocked)
    ]
    if not candidates:
        raise ValueError("no hay ningún clip utilizable en las rutas indicadas")

    chosen: list[Clip] = []
    total = 0.0
    for clip in candidates:
        if total >= target_seconds:
            break
        chosen.append(clip)
        total += clip.seconds

    gap = np.zeros(int(GAP_SECONDS * SAMPLE_RATE), dtype=np.float32)
    pieces: list[np.ndarray] = []
    for clip in chosen:
        pieces.extend((_normalize(clip.audio), gap))
    mixed = np.concatenate(pieces[:-1]) if pieces else np.zeros(0, dtype=np.float32)

    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), mixed, SAMPLE_RATE, subtype="PCM_16")
    log.info("referencia de voz: %d clips, %.1f s -> %s", len(chosen), total, out)
    return chosen


def _main() -> int:
    import argparse

    from ..paths import app_data_dir

    parser = argparse.ArgumentParser(
        prog="nova-voice",
        description="Prepara el audio de referencia para clonar una voz.")
    parser.add_argument("carpeta", help="carpeta con los archivos de voz")
    parser.add_argument("--salida", default=None,
                        help="WAV de salida (por defecto %%APPDATA%%/Nova/voices/reference.wav)")
    parser.add_argument("--segundos", type=float, default=TARGET_SECONDS)
    parser.add_argument("--excluir", nargs="*", default=[],
                        help="fragmentos de nombre a descartar, p. ej. Attack Dodge")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    carpeta = Path(args.carpeta)
    fuentes = [p for p in sorted(carpeta.rglob("*")) if p.suffix.lower() in
               (".ogg", ".wav", ".mp3", ".flac", ".m4a", ".opus")]
    if not fuentes:
        print(f"No encontré archivos de audio en {carpeta}")
        return 1

    salida = Path(args.salida) if args.salida else app_data_dir() / "voices" / "reference.wav"
    elegidos = build_reference(fuentes, salida, target_seconds=args.segundos,
                               exclude=args.excluir)
    print(f"\nEntraron {len(elegidos)} de {len(fuentes)} clips:")
    for clip in elegidos:
        print(f"  {clip.seconds:5.2f}s  {clip.path.name}")
    print(f"\nReferencia escrita en: {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
