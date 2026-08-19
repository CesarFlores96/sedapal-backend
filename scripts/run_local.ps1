[CmdletBinding()]
param(
  [int]$Port = 8000,
  [switch]$Reload = $true,
  [switch]$Force = $false
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$healthUrl = "http://127.0.0.1:$Port/health"

# Buscar si ya hay procesos escuchando en el puerto
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
  Select-Object -First 1

if ($listener) {
  $existingPid = $listener.OwningProcess
  if ($Force) {
    Write-Host "Terminando proceso previo en el puerto $Port (PID $existingPid)..." -ForegroundColor Cyan
    Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
  } else {
    try {
      $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
      if ($health.success -eq $true) {
        Write-Host "================================================================" -ForegroundColor Yellow
        Write-Host "FastAPI YA se encuentra en ejecución en $healthUrl (PID $existingPid)." -ForegroundColor Yellow
        Write-Host "No se puede iniciar una segunda instancia. (Usa -Force para reiniciar)" -ForegroundColor Yellow
        Write-Host "================================================================" -ForegroundColor Yellow
        exit 0
      }
    } catch {}

    Write-Host "El puerto $Port ya está ocupado por el PID $existingPid. Usa -Force para cerrarlo." -ForegroundColor Red
    exit 1
  }
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
  throw "No se encontró el entorno virtual en $pythonPath."
}

Set-Location -LiteralPath $projectRoot

$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "$Port")
if ($Reload) {
  $uvicornArgs += "--reload"
}

Write-Host "Iniciando FastAPI en http://127.0.0.1:$Port..." -ForegroundColor Green
& $pythonPath @uvicornArgs
