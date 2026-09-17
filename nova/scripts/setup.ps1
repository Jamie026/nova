# Instala TODO lo que Nova necesita en Windows 11, de una vez:
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 [-SinVoz] [-SinModelos]
#
#   1. entorno virtual de Python en core\.venv y sus dependencias
#   2. la voz clonada (PyTorch con CUDA + Chatterbox Turbo, ~4 GB) si hay GPU NVIDIA
#   3. el navegador de Playwright
#   4. los modelos: Whisper (transcripcion, juez y detector), palabra activadora
#      y la voz, para que el primer arranque no tenga que bajar nada
#   5. las dependencias de Electron (npm install)
#   6. el archivo .env a partir de .env.example
#
#   -SinVoz      salta el punto 2 (y la voz del 4). Sin GPU NVIDIA se salta solo.
#   -SinModelos  salta el punto 4 (se bajaran en el primer arranque).
#
# Es seguro repetirlo: lo que ya esta instalado o descargado no se repite.

param([switch]$SinVoz, [switch]$SinModelos)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$inicio = Get-Date

Write-Host "`n== 1/6 Python ==" -ForegroundColor Cyan
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "No encuentro Python. Instala 3.10 o superior desde python.org (marcando 'Add python.exe to PATH') y vuelve a ejecutar." }
$version = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
$minor = [int]$version.Split('.')[1]
if ($minor -lt 10) { throw "Python $version es demasiado viejo: hace falta 3.10 o superior." }
Write-Host "   Python $version"

Push-Location (Join-Path $root 'core')
try {
  Write-Host "`n== Entorno virtual y dependencias del nucleo ==" -ForegroundColor Cyan
  if (-not (Test-Path '.venv')) { & python -m venv .venv }
  $py = '.\.venv\Scripts\python.exe'
  & $py -m pip install --upgrade pip --quiet
  & $py -m pip install -r requirements.txt

  # La voz clonada solo tiene sentido con GPU NVIDIA (en CPU es inviable).
  $gpu = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  $conVoz = (-not $SinVoz) -and ($null -ne $gpu)
  Write-Host "`n== 2/6 Voz clonada (Chatterbox Turbo + PyTorch CUDA, ~4 GB) ==" -ForegroundColor Cyan
  if ($conVoz) {
    & $py -m pip install -r requirements-voice.txt
    # --no-deps porque chatterbox-tts fija torch 2.6 y aqui va 2.8 (ver requirements-voice.txt).
    & $py -m pip install --no-deps chatterbox-tts==0.1.7
  } elseif ($SinVoz) {
    Write-Host "   saltada (-SinVoz). Nova hablara con edge-tts."
  } else {
    Write-Host "   no veo una GPU NVIDIA (nvidia-smi): se salta. Nova hablara con edge-tts." -ForegroundColor Yellow
  }

  Write-Host "`n== 3/6 Navegador de Playwright ==" -ForegroundColor Cyan
  & $py -m playwright install chromium

  Write-Host "`n== 4/6 Modelos (Whisper, palabra activadora, voz) ==" -ForegroundColor Cyan
  if ($SinModelos) {
    Write-Host "   saltado (-SinModelos): se bajaran en el primer arranque."
  } elseif ($conVoz) {
    & $py -m nova.setup_models
  } else {
    & $py -m nova.setup_models --sin-voz
  }
} finally {
  Pop-Location
}

Write-Host "`n== 5/6 Dependencias de Electron ==" -ForegroundColor Cyan
Push-Location $root
try { & npm install } finally { Pop-Location }

Write-Host "`n== 6/6 Configuracion ==" -ForegroundColor Cyan
$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
  Copy-Item (Join-Path $root '.env.example') $envFile
  Write-Host "   Se creo .env - abrelo y pon tu DEEPSEEK_API_KEY." -ForegroundColor Yellow
} else {
  Write-Host "   .env ya existe; no se toca."
}
if ($conVoz) {
  $mine = Join-Path $env:APPDATA 'Nova\voices\reference.wav'
  $bundled = Join-Path $root 'core\nova\tts\voices\reference.wav'
  if (Test-Path $mine) {
    Write-Host "   Voz clonada: se usara tu referencia, $mine"
  } elseif (Test-Path $bundled) {
    Write-Host "   Voz clonada: se usara la referencia incluida en el repo. Para otra voz,"
    Write-Host "   pon un WAV en $mine  (INSTRUCCIONES.txt, seccion 7)"
  } else {
    Write-Host "   Falta la referencia de la voz (ni $bundled ni $mine):" -ForegroundColor Yellow
    Write-Host "   Nova hablara con edge-tts hasta que haya una (INSTRUCCIONES.txt, seccion 7)" -ForegroundColor Yellow
  }
}

$min = [int]((Get-Date) - $inicio).TotalMinutes
Write-Host "`nListo en $min min. Arranca con:  npm run dev" -ForegroundColor Green
