# Install Node + Python deps (Windows)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "=== setup:install ==="
npm install

$py = "python"
if (-not (Test-Path "venv")) {
    python -m venv venv
}
if (Test-Path "venv\Scripts\python.exe") {
    $py = "venv\Scripts\python.exe"
}

& $py -m pip install --upgrade pip uv
if (-not (Test-Path "requirements.lock")) {
    & $py -m uv pip compile requirements.txt -o requirements.lock
}
if (-not (Test-Path "requirements-dev.lock")) {
    & $py -m uv pip compile requirements-dev.txt -o requirements-dev.lock
}
# requirements.lock is compiled for Linux/Docker and pins uvloop (not supported on Windows).
if ($IsWindows -or $env:OS -like "*Windows*") {
    Write-Host "Skipping requirements.lock on Windows (use Docker for prod parity)."
} else {
    & $py -m uv pip sync requirements.lock
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
& $py -m uv pip sync requirements-dev.lock
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m pip install -e ".[dev]"

Write-Host ""
Write-Host "=== Done ==="
Write-Host "  venv:    .\venv\Scripts\activate"
Write-Host "  Signing: docs/SIGNING.md"
Write-Host "  Go-live: docs/GO_LIVE.md"
