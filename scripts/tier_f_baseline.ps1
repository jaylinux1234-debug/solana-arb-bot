# Tier F Step 1 — baseline metrics (Windows)
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

Write-Host "=== Tier F baseline ===" -ForegroundColor Cyan
npm run metrics:next
npm run probe:daily
npm run sim:breakeven:live
Write-Host "Done. Next: npm run sync:compose-env && npm run compose:prod:up" -ForegroundColor Green
