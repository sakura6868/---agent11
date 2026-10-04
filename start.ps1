param(
    [ValidateRange(1, 65535)][int]$Port = 8000,
    [switch]$Docker
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
}

if ($Docker) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is not installed. Run without -Docker for local Python."
    }
    docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0) { throw "Start Docker Desktop, then retry with -Docker." }
    $env:HOST_PORT = "$Port"
    docker compose up --build
    exit $LASTEXITCODE
}

if (-not (Test-Path "venv\Scripts\python.exe")) {
    python -m venv venv
    if ($LASTEXITCODE -ne 0) { throw "Python virtual environment creation failed." }
}

& .\venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
Write-Host "Campus agent: http://127.0.0.1:$Port/"
& .\venv\Scripts\python.exe -m uvicorn api:app --app-dir src --host 127.0.0.1 --port $Port
