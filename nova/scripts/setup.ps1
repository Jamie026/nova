# Preparación del entorno en Windows 11.
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

Write-Host "`n== Comprobando Python ==" -ForegroundColor Cyan
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw "No encuentro Python. Instala 3.10 o superior desde python.org y vuelve a ejecutar." }
$version = (& python -c "import sys; print('.'.join(map(str, sys.version_info[:2])))")
Write-Host "   Python $version"

Write-Host "`n== Entorno virtual del nucleo ==" -ForegroundColor Cyan
Push-Location (Join-Path $root 'core')
if (-not (Test-Path '.venv')) { & python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Write-Host "`n== Navegador de Playwright ==" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m playwright install chromium

# Los modelos de la palabra activadora sin clave (7 MB). Bajarlos aqui evita que
# el primer arranque se quede esperando red justo cuando el usuario mira.
Write-Host "`n== Modelos de la palabra activadora ==" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -c "import openwakeword.utils as u; u.download_models(['hey_jarvis'])"
Pop-Location

Write-Host "`n== Dependencias de Electron ==" -ForegroundColor Cyan
Push-Location $root
& npm install
Pop-Location

$envFile = Join-Path $root '.env'
if (-not (Test-Path $envFile)) {
  Copy-Item (Join-Path $root '.env.example') $envFile
  Write-Host "`n   Se creo .env — abrelo y pon tu DEEPSEEK_API_KEY." -ForegroundColor Yellow
}

Write-Host "`nListo. Arranca con:  npm run dev" -ForegroundColor Green
