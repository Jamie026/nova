"""Regenera desktop/src/main/tray-icon.ts a partir del retrato del avatar.

Uso:  python scripts/make-tray-icon.py [--crop 145,0,285,140]

Recorta la cabeza del retrato (a 32 px la figura completa es una mancha) y la
incrusta como data URL en TypeScript. Requiere Pillow, que no está en
requirements.txt porque sólo hace falta para esta tarea puntual:

    pip install pillow
"""
from __future__ import annotations

import argparse
import base64
import io
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "desktop" / "src" / "renderer" / "overlay" / "assets" / "nova-avatar.webp"
TARGET = ROOT / "desktop" / "src" / "main" / "tray-icon.ts"

HEADER = """/**
 * Icono de la bandeja del sistema, incrustado como data URL.
 *
 * Es un recorte de la cabeza del retrato que usa el avatar: a 32 px la figura
 * completa no se distingue, la cara sí. Va embebido y no como archivo suelto
 * para que el empaquetado no dependa de copiar assets a la carpeta correcta.
 * Se regenera con scripts/make-tray-icon.py si cambias el retrato.
 */
export const TRAY_ICON_DATA_URL =
  'data:image/png;base64,' +
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crop", default="145,0,285,140", help="left,top,right,bottom del recorte")
    parser.add_argument("--size", type=int, default=32)
    args = parser.parse_args()

    box = tuple(int(v) for v in args.crop.split(","))
    image = Image.open(SOURCE).convert("RGBA").crop(box).resize((args.size, args.size), Image.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode()

    lines = [encoded[i:i + 100] for i in range(0, len(encoded), 100)]
    body = "  '" + "' +\n  '".join(lines) + "';\n"
    TARGET.write_text(HEADER + body, encoding="utf-8")
    print(f"{TARGET.relative_to(ROOT)} actualizado ({len(encoded)} caracteres base64)")


if __name__ == "__main__":
    main()
