"""Ajustar XTTS-v2 a una voz concreta:  npm run voice:train -- "<carpeta con audios>"

Esto es lo único que hace que la voz clonada se parezca MÁS a la referencia
que el clonado zero-shot: en vez de copiar una foto de la voz en cada frase, el
modelo aprende esa voz. Lo que NO sirve es entrenar sobre audio generado por el
propio clonado: eso es fotocopiar una fotocopia, y por muchos minutos que se
generen no hay más información de la voz real que la que ya había en la
referencia. Aquí sólo entran grabaciones reales.

Tres avisos honestos, todos medidos (parecido = coseno con la referencia según
el codificador de hablante de XTTS, que no se entrena; se entiende = cuánto
de la frase recupera Whisper; 20–30 frases por fila, mismas semillas):

  * Con 206 s de audio real (3,4 min, 70 frases) NADA supera al zero-shot.
        zero-shot                          parecido 0,595   se entiende 0,93
        sólo ruta de la voz (por defecto)  parecido 0,574   se entiende 0,92
        + últimas 8 capas del GPT          parecido 0,547   se entiende 0,80
        ídem con lotes de 8 en vez de 24   parecido 0,489   se entiende 0,57
    Con 61 s era igual o peor. Este script existe para cuando haya MUCHO más
    audio (decenas de minutos), y aun así el resultado se mide antes de
    adoptarlo; no se da por bueno a ciegas.
  * Tocar las capas del GPT rompe la inteligibilidad por arquitectura, no por
    falta de datos: el vocoder lee el estado oculto del GPT, no los códigos
    que la pérdida ancla (ver _freeze). Por eso el valor por defecto es 0
    capas: sólo la ruta que convierte la referencia en identidad de voz, que
    no puede estropear nada… ni, con este audio, mejorarlo.
  * Ajustar el GPT de XTTS pide mucha memoria de vídeo. Con 6 GB va justísimo:
    lote de 1, precisión mixta y acumulación de gradiente. Si no cabe, este
    script lo dice al primer paso, no tras una hora. La acumulación además
    importa para el resultado: AdamW mueve cada peso ~lr por paso haga lo que
    haga el gradiente, así que con lotes de 8 cada paso es casi ruido y el
    daño crece con el número de pasos; con 24 (más cerca de los 252 de la
    receta de coqui) el mismo ajuste pasó de entenderse 0,57 a 0,80.

Flujo:
  1. Corta cada archivo en frases por los silencios y transcribe cada trozo con
     Whisper: metadata.csv en formato coqui (audio|texto|hablante). Sólo cuando
     un trozo sin silencios pasa de 11 s (diálogo seguido) se subdivide con las
     marcas de tiempo de Whisper. Cortar por energía primero importa con vídeos
     de juego: Whisper juntaba 1 s de voz con 10–30 s de fondo casi mudo en un
     mismo segmento y se perdía un tercio de la voz.
  0. Si la grabación lleva música o efectos de fondo, sepárala antes con
     Demucs (está en requirements-voice):
       python -m demucs --two-stems=vocals -n htdemucs -d cuda -o <salida> <archivo>
     y entrena con el vocals.wav que deja. Entrenar con música debajo enseña al
     modelo a poner música debajo.
  2. Entrena el GPT unas pocas épocas partiendo de los pesos de XTTS-v2.
  3. Deja el modelo en %APPDATA%/Nova/voices/finetuned/ y explica cómo activarlo.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("nova.tts.finetune")

SR = 22_050
MIN_CLIP_S = 1.0
MAX_CLIP_S = 11.0
SILENCE_DB = 30        # por debajo del pico, para cortar por silencios
JOIN_GAP_S = 0.5       # pausas más cortas no separan frases
PAD_S = 0.12           # aire que se deja a cada lado del corte
MODEL_DIR = Path.home() / "AppData" / "Local" / "tts" / "tts_models--multilingual--multi-dataset--xtts_v2"


# ----------------------------------------------------------------------------
# 1. Dataset
# ----------------------------------------------------------------------------
def _split_by_silence(audio24) -> list[tuple[int, int]]:
    """Trozos (inicio, fin) en muestras de 24 kHz, cortados por los silencios.

    Pausas menores de JOIN_GAP_S no separan; se junta mientras el trozo no pase
    de MAX_CLIP_S. Los trozos que aun así pasan se devuelven enteros y los
    subdivide Whisper después.
    """
    import librosa
    import numpy as np

    # El umbral es relativo al pico: un archivo mudo daría un solo trozo "alto".
    if not len(audio24) or float(np.abs(audio24).max()) < 0.005:
        return []
    intervals = librosa.effects.split(audio24, top_db=SILENCE_DB, frame_length=1024, hop_length=256)
    merged: list[list[int]] = []
    for start, end in intervals:
        if merged and start - merged[-1][1] < JOIN_GAP_S * 24000 \
                and end - merged[-1][0] <= MAX_CLIP_S * 24000:
            merged[-1][1] = int(end)
        else:
            merged.append([int(start), int(end)])
    pad = int(PAD_S * 24000)
    return [(max(0, s - pad), min(len(audio24), e + pad)) for s, e in merged]


def _to_16k(audio24):
    import numpy as np

    return np.interp(np.linspace(0, len(audio24) - 1, int(len(audio24) * 16000 / 24000)),
                     np.arange(len(audio24)), audio24).astype(np.float32)


def build_dataset(sources: list[Path], out: Path, language: str = "en") -> tuple[Path, float]:
    """Corta y transcribe. Devuelve (ruta de metadata.csv, segundos totales)."""
    import soundfile as sf

    from ..stt.faster_whisper_stt import _prepare_cuda_libs
    from ..stt.hallucinations import is_hallucination
    from .voice_profile import _to_mono_24k

    _prepare_cuda_libs()
    from faster_whisper import WhisperModel
    from scipy.signal import resample_poly

    wavs = out / "wavs"
    wavs.mkdir(parents=True, exist_ok=True)
    whisper = WhisperModel("medium", device="cuda", compute_type="int8_float16")
    rows: list[tuple[str, str]] = []
    total = 0.0

    def transcribe(piece24, vad: bool):
        return whisper.transcribe(_to_16k(piece24), language=language, beam_size=5, vad_filter=vad,
                                  temperature=0.0, condition_on_previous_text=False)[0]

    for path in sources:
        try:
            audio24 = _to_mono_24k(path)
        except Exception as exc:
            log.warning("no pude leer %s: %s", path.name, exc)
            continue
        clips: list[tuple[int, int, str]] = []
        for start, end in _split_by_silence(audio24):
            dur = (end - start) / 24000
            if dur < MIN_CLIP_S:
                continue
            piece = audio24[start:end]
            if dur <= MAX_CLIP_S:
                text = " ".join(s.text.strip() for s in transcribe(piece, vad=False)).strip()
                clips.append((start, end, text))
                continue
            # Habla seguida sin pausas claras: que Whisper ponga los cortes.
            for seg in transcribe(piece, vad=True):
                if MIN_CLIP_S <= seg.end - seg.start <= MAX_CLIP_S:
                    clips.append((start + int(seg.start * 24000), start + int(seg.end * 24000),
                                  seg.text.strip()))
        for k, (start, end, text) in enumerate(clips):
            dur = (end - start) / 24000
            if not text or is_hallucination(text):
                continue
            piece22 = resample_poly(audio24[start:end], 147, 160).astype("float32")   # 24000 -> 22050
            name = f"{path.stem}_{k:03d}.wav"
            sf.write(str(wavs / name), piece22, SR, subtype="PCM_16")
            rows.append((f"wavs/{name}", text))
            total += dur
        log.info("  %s: %d frases", path.name, len(clips))
    meta = out / "metadata.csv"
    with meta.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="|")
        writer.writerow(["audio_file", "text", "speaker_name"])
        for audio, text in rows:
            writer.writerow([audio, text, "voz"])
    log.info("dataset: %d frases, %.0f s -> %s", len(rows), total, meta)
    return meta, total


# ----------------------------------------------------------------------------
# 2. Entrenamiento
# ----------------------------------------------------------------------------
TRAINABLE_LAYERS = 0   # capas del GPT que se tocan; 0 = sólo la ruta de la voz. Ver _freeze


def _freeze(model, trainable_layers: int) -> tuple[int, int]:
    """Deja entrenable la ruta de la voz y, si se pide, las últimas capas del GPT.

    Por defecto (0 capas) se entrena SÓLO conditioning_encoder + conditioning_
    perceiver (46 M): los que convierten el audio de referencia en la
    «identidad» de la voz que el GPT recibe como prefijo. Es lo único que,
    medido, mejora el parecido sin estropear nada:

        zero-shot                      parecido 0,562  se entiende 0,896
        sólo ruta de la voz            parecido 0,583  se entiende 0,956
        + últimas 8 capas + mel_head   parecido 0,547  se entiende 0,797

    Tocar las capas del GPT empeora la inteligibilidad SIEMPRE, con 61 s y con
    206 s, y la razón es de arquitectura, no de datos: el vocoder (HiFi-GAN,
    congelado) no lee los códigos que predice mel_head, lee el estado oculto
    del GPT tal cual. La pérdida sólo ancla mel_head(latente), así que las
    capas pueden mover el latente por donde a mel_head le da igual y la pérdida
    baja mientras el vocoder recibe algo que nunca vio: sale «T-T-Ten-Ses».
    Con --capas N se puede probar igualmente, pero hay que medirlo.

    Lo de congelar también es lo que hace que quepa: AdamW guarda dos copias
    más de cada parámetro que entrena, y con los 441 M del GPT entero eso son
    3,5 GB extra que en una tarjeta de 6 GB reventaban al primer paso.
    """
    gpt = model.xtts.gpt
    for p in gpt.parameters():
        p.requires_grad = False
    keep = [gpt.conditioning_encoder]
    if getattr(gpt, "conditioning_perceiver", None) is not None:
        keep.append(gpt.conditioning_perceiver)
    if trainable_layers > 0:
        keep += [gpt.mel_head, gpt.gpt.ln_f, *gpt.gpt.h[-trainable_layers:]]
    for module in keep:
        for p in module.parameters():
            p.requires_grad = True
    trainable = sum(p.numel() for p in gpt.parameters() if p.requires_grad)
    total = sum(p.numel() for p in gpt.parameters())
    return trainable, total


def train(dataset_dir: Path, out: Path, epochs: int, lr: float, language: str,
          batch_size: int = 1, grad_accum: int = 24, max_steps: int | None = None,
          trainable_layers: int = TRAINABLE_LAYERS) -> Path:
    os.environ.setdefault("COQUI_TOS_AGREED", "1")
    # Sin fragmentación: con la memoria tan justa, los huecos entre bloques
    # reservados son la diferencia entre caber y no.
    os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    from trainer import Trainer, TrainerArgs
    from TTS.config.shared_configs import BaseDatasetConfig
    from TTS.tts.datasets import load_tts_samples
    from TTS.tts.layers.xtts.trainer.gpt_trainer import GPTArgs, GPTTrainer, GPTTrainerConfig
    from TTS.tts.models.xtts import XttsAudioConfig

    for needed in ("model.pth", "dvae.pth", "mel_stats.pth", "vocab.json"):
        if not (MODEL_DIR / needed).exists():
            raise FileNotFoundError(f"falta {needed} en {MODEL_DIR}")

    dataset = BaseDatasetConfig(formatter="coqui", dataset_name="voz", path=str(dataset_dir),
                                meta_file_train="metadata.csv", language=language)
    model_args = GPTArgs(
        max_conditioning_length=132300,     # 6 s
        min_conditioning_length=66150,      # 3 s
        max_wav_length=198450,              # 9 s: menos activaciones en memoria
        max_text_length=200,
        mel_norm_file=str(MODEL_DIR / "mel_stats.pth"),
        dvae_checkpoint=str(MODEL_DIR / "dvae.pth"),
        xtts_checkpoint=str(MODEL_DIR / "model.pth"),
        tokenizer_file=str(MODEL_DIR / "vocab.json"),
        gpt_num_audio_tokens=1026, gpt_start_audio_token=1024, gpt_stop_audio_token=1025,
        gpt_use_masking_gt_prompt_approach=True, gpt_use_perceiver_resampler=True,
    )
    config = GPTTrainerConfig(
        output_path=str(out), model_args=model_args, run_name="nova-voz",
        audio=XttsAudioConfig(sample_rate=SR, dvae_sample_rate=SR, output_sample_rate=24000),
        batch_size=batch_size, eval_batch_size=1, batch_group_size=0,
        num_loader_workers=0, eval_split_max_size=4, eval_split_size=0.1,
        print_step=17, plot_step=100000, log_model_step=100000, save_step=17,
        save_n_checkpoints=2, save_checkpoints=True, print_eval=False,
        epochs=epochs, mixed_precision=True, precision="bf16",
        optimizer="AdamW", optimizer_wd_only_on_weights=True,
        optimizer_params={"betas": [0.9, 0.96], "eps": 1e-8, "weight_decay": 1e-2},
        lr=lr, lr_scheduler="MultiStepLR",
        lr_scheduler_params={"milestones": [50000], "gamma": 0.5, "last_epoch": -1},
        test_sentences=[],
    )
    model = GPTTrainer.init_from_config(config)
    trainable, total = _freeze(model, trainable_layers)
    log.info("entrenables: %.0f M de %.0f M parámetros", trainable / 1e6, total / 1e6)
    train_samples, eval_samples = load_tts_samples(
        [dataset], eval_split=True, eval_split_max_size=config.eval_split_max_size,
        eval_split_size=config.eval_split_size)
    args = TrainerArgs(restore_path=None, skip_train_epoch=False, start_with_eval=False,
                       grad_accum_steps=grad_accum)
    trainer = Trainer(args, config, output_path=str(out), model=model,
                      train_samples=train_samples, eval_samples=eval_samples)
    if max_steps:
        trainer.total_steps_done = 0
        original = trainer.train_step

        def limited(*a, **k):
            if trainer.total_steps_done >= max_steps:
                raise KeyboardInterrupt("tope de pasos de prueba")
            return original(*a, **k)
        trainer.train_step = limited
    trainer.fit()
    return _compact(out)


def _compact(run_root: Path) -> Path:
    """Del checkpoint del entrenador (3,2 GB con optimizador) al modelo (1,8 GB).

    El entrenador guarda también los momentos de AdamW y el dVAE, que sólo
    sirven para seguir entrenando. Para hablar hace falta el modelo y nada más;
    se extrae, se guarda en finetuned/model.pth y se borran los checkpoints.
    """
    import shutil
    import torch

    runs = sorted(p for p in run_root.iterdir() if p.is_dir())
    if not runs:
        raise FileNotFoundError("el entrenamiento no dejó ningún resultado")
    best = runs[-1] / "best_model.pth"
    if not best.exists():
        candidates = sorted(runs[-1].glob("checkpoint_*.pth"))
        if not candidates:
            raise FileNotFoundError("no hay checkpoint en " + str(runs[-1]))
        best = candidates[-1]
    state = torch.load(best, map_location="cpu", weights_only=False)
    weights = state["model"] if isinstance(state, dict) and "model" in state else state
    weights = {k[len("xtts."):]: v for k, v in weights.items() if k.startswith("xtts.")}
    # voices/finetune/run -> voices/finetuned, al lado de reference.wav
    target = run_root.parents[1] / "finetuned"
    target.mkdir(parents=True, exist_ok=True)
    torch.save({"model": weights}, target / "model.pth")
    shutil.copy(MODEL_DIR / "config.json", target / "config.json")
    shutil.copy(MODEL_DIR / "vocab.json", target / "vocab.json")
    shutil.rmtree(run_root, ignore_errors=True)
    log.info("modelo ajustado guardado en %s (%.1f GB)", target,
             (target / "model.pth").stat().st_size / 2**30)
    return target


def _main() -> int:
    parser = argparse.ArgumentParser(description="Ajusta XTTS-v2 a una voz con grabaciones reales.")
    parser.add_argument("carpeta")
    parser.add_argument("--idioma", default="en")
    parser.add_argument("--epocas", type=int, default=10)
    parser.add_argument("--capas", type=int, default=TRAINABLE_LAYERS,
                        help="capas del GPT a entrenar; 0 = sólo la ruta de la voz (lo único que no rompe el habla)")
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--acumular", type=int, default=24,
                        help="frases por paso del optimizador; menos de 24 mete ruido y empeora")
    parser.add_argument("--solo-dataset", action="store_true")
    parser.add_argument("--pasos-prueba", type=int, default=0,
                        help="para probar si cabe en la GPU: se para tras N pasos")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)

    from ..paths import app_data_dir
    base = app_data_dir() / "voices" / "finetune"
    fuentes = [p for p in sorted(Path(args.carpeta).rglob("*"))
               if p.suffix.lower() in (".ogg", ".wav", ".mp3", ".flac", ".m4a", ".opus")]
    if not fuentes:
        print("No hay audios en", args.carpeta)
        return 1
    meta, total = build_dataset(fuentes, base / "dataset", args.idioma)
    print(f"\nDataset: {total:.0f} s de voz real en {meta}")
    if total < 600:
        print("AVISO: con 3,4 minutos de audio real, medido, el ajuste no superó al zero-shot.\n"
              "       Con menos de diez, compara antes de activarlo; no lo des por bueno.")
    if args.solo_dataset:
        return 0
    out = train(base / "dataset", base / "run", args.epocas, args.lr, args.idioma,
                grad_accum=args.acumular, max_steps=args.pasos_prueba or None,
                trainable_layers=args.capas)
    print(f"\nModelo ajustado en {out}")
    print('Para usarlo, en settings.json:  "tts": { "model_path": "' + str(out).replace("\\", "/") + '" }')
    print("Y COMPÁRALO antes de quedártelo: con 3,4 min de audio real el ajustado NO superó al zero-shot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
