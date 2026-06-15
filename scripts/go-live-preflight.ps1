# Final preflight (Windows)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = "python"
if (Test-Path "venv\Scripts\python.exe") { $py = "venv\Scripts\python.exe" }

Write-Host "=== go-live:preflight ==="
npm run secrets:sync-local
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm run sync:compose-env
& $py scripts/validate_go_live_env.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py scripts/go_live_preflight_checks.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm run test:py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm run compile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "=== Preflight OK ==="
Write-Host "  Deploy (optional): npm run deploy:secure"
Write-Host "  Launch:            npm run compose:prod:up"
