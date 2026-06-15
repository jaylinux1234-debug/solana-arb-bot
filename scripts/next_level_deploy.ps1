# Windows equivalent of scripts/next_level_deploy.sh
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "Deploying Next-Level Enhancements..."

if (Test-Path "scripts/enhanced_monitor.py") {
    $appScripts = "/app/scripts"
    if (Test-Path $appScripts) {
        Copy-Item "scripts/enhanced_monitor.py" $appScripts -Force
    }
}

npm run sync:compose-env
npm run compose:prod:up

Write-Host "Next-level features deployed."
npm run metrics:next
Write-Host "Run: python scripts/enhanced_monitor.py"
