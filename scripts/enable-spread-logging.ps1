# Windows equivalent of scripts/enable-spread-logging.sh
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "Enabling better spread logging..."

$line = "CEX_DEX_LOG_NEAR_MISSES=true"
if (Test-Path .env) {
  $content = Get-Content .env -Raw
  if ($content -match '(?m)^CEX_DEX_LOG_NEAR_MISSES=') {
    $content = $content -replace '(?m)^CEX_DEX_LOG_NEAR_MISSES=.*', $line
    Set-Content .env $content -NoNewline
  } else {
    Add-Content .env "`n$line"
  }
} else {
  throw "Missing .env"
}

Write-Host "Done. Restarting monitor..."
npm run sync:compose-env
npm run compose:prod:restart
