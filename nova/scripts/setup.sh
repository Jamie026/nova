#!/usr/bin/env bash
# Instala TODO lo que Nova necesita en macOS o Linux, de una vez:
#
#   bash scripts/setup.sh [--sin-voz] [--sin-modelos]
#
# El control de ventanas aún no está implementado fuera de Windows, pero el
# núcleo, el navegador y la interfaz sí funcionan. La voz clonada sólo se
# instala si hay GPU NVIDIA (nvidia-smi); en CPU es inviable.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
sin_voz=0; sin_modelos=0
for arg in "$@"; do
  case "$arg" in
    --sin-voz) sin_voz=1 ;;
    --sin-modelos) sin_modelos=1 ;;
    *) echo "opción desconocida: $arg" >&2; exit 2 ;;
  esac
done

echo "== 1/6 Entorno virtual y dependencias del núcleo =="
cd "$root/core"
[ -d .venv ] || python3 -m venv .venv
py=./.venv/bin/python
$py -m pip install --upgrade pip --quiet
$py -m pip install -r requirements.txt

con_voz=0
echo "== 2/6 Voz clonada (Chatterbox Turbo + PyTorch CUDA, ~4 GB) =="
if [ "$sin_voz" = 1 ]; then
  echo "   saltada (--sin-voz). Nova hablará con edge-tts."
elif command -v nvidia-smi >/dev/null 2>&1; then
  $py -m pip install -r requirements-voice.txt
  # --no-deps porque chatterbox-tts fija torch 2.6 y aquí va 2.8 (ver requirements-voice.txt).
  $py -m pip install --no-deps chatterbox-tts==0.1.7
  con_voz=1
else
  echo "   no veo una GPU NVIDIA (nvidia-smi): se salta. Nova hablará con edge-tts."
fi

echo "== 3/6 Navegador de Playwright =="
$py -m playwright install chromium

echo "== 4/6 Modelos (Whisper, palabra activadora, voz) =="
if [ "$sin_modelos" = 1 ]; then
  echo "   saltado (--sin-modelos): se bajarán en el primer arranque."
elif [ "$con_voz" = 1 ]; then
  $py -m nova.setup_models
else
  $py -m nova.setup_models --sin-voz
fi

echo "== 5/6 Dependencias de Electron =="
cd "$root"
npm install

echo "== 6/6 Configuración =="
[ -f .env ] || { cp .env.example .env; echo "   Se creó .env — pon tu DEEPSEEK_API_KEY."; }
if [ "$con_voz" = 1 ]; then
  echo "   Voz clonada: usa la referencia incluida en el repo (core/nova/tts/voices/reference.wav)."
  echo "   Para otra voz, pon un WAV en ~/.config/nova/voices/reference.wav (INSTRUCCIONES.txt, §7)."
fi

echo "Listo. Arranca con: npm run dev"
