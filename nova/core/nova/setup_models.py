"""Descarga por adelantado todo lo que Nova bajaría en el primer arranque.

    python -m nova.setup_models [--sin-voz]

Sin esto, el primer «Corrin» de un equipo recién instalado se queda esperando
red: el Whisper de la transcripción (~1,5 GB), el del juez de frases cortas y
el del detector de la palabra activadora, y la voz clonada (~2 GB) si está
instalada. Se corre desde scripts/setup.ps1, y es inocuo repetirlo: lo que ya
está en la caché de Hugging Face no se vuelve a bajar.
"""
from __future__ import annotations

import argparse
import sys

from .config import Config


def _whisper(sizes: list[str]) -> None:
    from faster_whisper import download_model

    for size in sizes:
        print(f"  whisper {size}…", end=" ", flush=True)
        path = download_model(size)
        print(path)


def _wake_word() -> None:
    try:
        import openwakeword.utils as utils
    except ImportError:
        print("  openwakeword no está instalado; se salta")
        return
    print("  openwakeword hey_jarvis…", end=" ", flush=True)
    utils.download_models(["hey_jarvis"])
    print("ok")


def _chatterbox() -> bool:
    try:
        from chatterbox.tts_turbo import REPO_ID
        from huggingface_hub import snapshot_download
    except ImportError:
        print("  chatterbox-tts no está instalado (npm run deps:voice); se salta la voz")
        return False
    print(f"  {REPO_ID}…", end=" ", flush=True)
    # Los mismos patrones que ChatterboxTurboTTS.from_pretrained, para que el
    # primer arranque encuentre exactamente lo que busca.
    path = snapshot_download(repo_id=REPO_ID,
                             allow_patterns=["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"])
    print(path)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Descarga los modelos que Nova necesita.")
    parser.add_argument("--sin-voz", action="store_true", help="no bajar la voz clonada")
    args = parser.parse_args(argv)

    config = Config.load()
    from .stt.faster_whisper_stt import JUDGE_MODEL

    sizes: list[str] = []
    for size in (config.get("stt.model", "medium"), JUDGE_MODEL, config.get("wake.model", "tiny")):
        if size not in sizes:
            sizes.append(size)

    print("== Modelos de Whisper ==")
    _whisper(sizes)
    print("== Palabra activadora ==")
    _wake_word()
    if not args.sin_voz:
        print("== Voz clonada (Chatterbox Turbo) ==")
        _chatterbox()
    print("Listo: el primer arranque no tendrá que descargar nada.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
