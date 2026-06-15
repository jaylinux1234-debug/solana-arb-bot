# Prod go-live secrets (Windows)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "=== go-live:secrets (PowerShell) ==="

if (Get-Command bash -ErrorAction SilentlyContinue) {
    $bashOk = $true
    try {
        bash scripts/setup-secrets.sh 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { $bashOk = $false }
        bash scripts/decrypt-secrets.sh 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { $bashOk = $false }
    } catch {
        $bashOk = $false
    }
    if (-not $bashOk) {
        Write-Host "  bash setup failed - using node bootstrap"
        node scripts/init-secrets-templates.mjs
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
} else {
    node scripts/init-secrets-templates.mjs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

node scripts/sync-secrets-local.mjs --migrate-env
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$py = "python"
if (Test-Path "venv\Scripts\python.exe") { $py = "venv\Scripts\python.exe" }
& $py scripts/validate_go_live_env.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$names = @(
    "private_key", "private_key.txt", "private_key_cex_dex", "jupiter_api_key",
    "openai_api_key", "backpack_secret", "backpack_api_key", "helius_api_key",
    "oneinch_api_key.txt", "cow_api_key.txt", "pagerduty_routing_key.txt"
)
$missing = 0
foreach ($n in $names) {
    $p = "secrets\$n"
    $local = "secrets\.local\$n"
    $ok = (Test-Path $p) -and ((Get-Item $p).Length -gt 0)
    if (-not $ok) {
        $ok = (Test-Path $local) -and ((Get-Item $local).Length -gt 0)
    }
    if (-not $ok) {
        Write-Warning "secrets/$n is missing or empty (check secrets/.local/)"
        $missing++
    } else {
        Write-Host "  ok secrets/$n"
    }
}

npm run sync:compose-env

Write-Host "=== go-live:secrets complete ==="
if ($missing -gt 0) {
    Write-Host "  Fill empty secrets/* before: npm run compose:prod:up"
} else {
    Write-Host "  Next: npm run compose:prod:up"
}
