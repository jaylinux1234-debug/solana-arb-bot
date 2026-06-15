# Phase 1 re-arm: sync inventory, withdraw USDC, restart stack, tail MEV log.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

Write-Host "=== Phase 1 Re-Arm ===" -ForegroundColor Cyan

npm run inventory:usdc-sync:force
if ($LASTEXITCODE -ne 0) { Write-Warning "inventory sync returned $LASTEXITCODE" }

python scripts/v2_withdraw_usdc.py --execute --amount 35
if ($LASTEXITCODE -ne 0) { Write-Warning "Withdraw skipped or failed — check Backpack balance" }

python scripts/track_capital_delta.py --action rearm_post_withdraw
npm run v2:balance

npm run sync:compose-env
npm run compose:prod:restart

Write-Host "Monitoring live fills (Ctrl+C to stop)..." -ForegroundColor Green
$env:MEV_WATCH_TAIL = "100"
npm run mev:watch:live
