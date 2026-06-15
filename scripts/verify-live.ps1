# Windows equivalent of verify-live.sh
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "=== SOLANA ARB BOT LIVE VERIFICATION ==="

Write-Host "`n1. Health Check:"
npm run health:quick

Write-Host "`n2. Recent Logs (last 30 lines):"
docker logs solana-arb-monitor --tail 30 2>&1

Write-Host "`n3. Checking for key terms:"
$terms = docker logs solana-arb-monitor --tail 100 2>&1 |
  Select-String -Pattern "quote_only|Ledger|signer|opportunity|AI approve|Backpack"
if ($terms) { $terms } else { Write-Host "No key terms found in last 100 lines" }

Write-Host "`nVerification complete. Monitor with: npm run logs:tail"
