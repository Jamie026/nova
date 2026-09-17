# Primer arranque de la version instalada (Nova-Setup.exe).
#
# El instalador lleva el nucleo Python en resources\core pero NO su entorno
# virtual: son 5 GB con PyTorch y un venv de Windows no se puede copiar de una
# maquina a otra (apunta al Python con el que se creo). Asi que se crea aqui,
# dentro de la carpeta de instalacion, la primera vez que se abre Nova. Electron
# lanza este script en una ventana visible y arranca el nucleo cuando termina.
#
#   powershell -ExecutionPolicy Bypass -File instalar-nucleo.ps1 [-Voz]
#
# -Voz instala ademas la voz clonada (Chatterbox + PyTorch con CUDA, ~4 GB).

param([switch]$Voz)

$ErrorActionPreference = 'Stop'
$resources = Split-Path -Parent $PSScriptRoot
$core = Join-Path $resources 'core'

Write-Host "`n== Nova: preparando el nucleo (solo la primera vez) ==" -ForegroundColor Cyan
Write-Host "   Carpeta: $core"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
  Write-Host "`nNo encuentro Python. Instala Python 3.13 desde python.org marcando" -ForegroundColor Red
  Write-Host "'Add python.exe to PATH', y vuelve a abrir Nova." -ForegroundColor Red
  Read-Host "`nPulsa Enter para cerrar"
  exit 1
}
$version = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
Write-Host "   Python $version"

Push-Location $core
try {
  if (-not (Test-Path '.venv')) { & python -m venv .venv }
  & .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
  Write-Host "`n== Dependencias del nucleo ==" -ForegroundColor Cyan
  & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
  if ($Voz) {
    Write-Host "`n== Voz clonada (Chatterbox + PyTorch, ~4 GB) ==" -ForegroundColor Cyan
    & .\.venv\Scripts\python.exe -m pip install -r requirements-voice.txt
    & .\.venv\Scripts\python.exe -m pip install --no-deps chatterbox-tts==0.1.7
  }
  Write-Host "`n== Navegador de Playwright ==" -ForegroundColor Cyan
  & .\.venv\Scripts\python.exe -m playwright install chromium
  Write-Host "`n== Modelos de la palabra activadora ==" -ForegroundColor Cyan
  & .\.venv\Scripts\python.exe -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"
} finally {
  Pop-Location
}

# La configuracion vive en %APPDATA%\Nova, que es donde el nucleo guarda todo.
$appData = Join-Path $env:APPDATA 'Nova'
New-Item -ItemType Directory -Force $appData | Out-Null
$envFile = Join-Path $appData '.env'
if (-not (Test-Path $envFile)) {
  Copy-Item (Join-Path $resources '.env.example') $envFile
  Write-Host "`nSe creo $envFile" -ForegroundColor Yellow
  Write-Host "Se abre ahora: pon tu DEEPSEEK_API_KEY, guarda y cierra el Bloc de notas." -ForegroundColor Yellow
  Start-Process -Wait notepad.exe $envFile
}

Write-Host "`nListo. Nova arranca sola al cerrar esta ventana." -ForegroundColor Green
Start-Sleep -Seconds 2
