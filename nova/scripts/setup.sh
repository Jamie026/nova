#!/usr/bin/env bash
# Preparación en macOS o Linux. El control de ventanas aún no está implementado
# fuera de Windows, pero el núcleo, el navegador y la interfaz sí funcionan.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"

echo "== Entorno virtual del núcleo =="
cd "$root/core"
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip --quiet
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m playwright install chromium
# Modelos de la palabra activadora sin clave (7 MB), para que el primer arranque
# no dependa de la red.
./.venv/bin/python -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"

echo "== Dependencias de Electron =="
cd "$root"
npm install
[ -f .env ] || { cp .env.example .env; echo "   Se creó .env — pon tu DEEPSEEK_API_KEY."; }

echo "Listo. Arranca con: npm run dev"
