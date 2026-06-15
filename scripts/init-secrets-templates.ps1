# Create secrets/ + .txt templates (Windows)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

New-Item -ItemType Directory -Force -Path "secrets", "secrets\encrypted" | Out-Null

function Write-Template($path, $body) {
    if ((Test-Path $path) -and ((Get-Item $path).Length -gt 0)) {
        Write-Host "  skip $path (already has content)"
        return
    }
    Set-Content -Path $path -Value $body -NoNewline
    Write-Host "  created $path"
}

Write-Template "secrets\private_key.txt" @"
# ONLY if NOT using Ledger. Otherwise leave empty or use hardware.
your_private_key_here_without_0x_if_needed
"@

Write-Template "secrets\oneinch_api_key.txt" "your_1inch_api_key"
Write-Template "secrets\cow_api_key.txt" "your_cow_api_key"
Write-Template "secrets\pagerduty_routing_key.txt" "your_pagerduty_integration_key"

foreach ($n in @("private_key", "private_key_cex_dex", "jupiter_api_key", "openai_api_key", "backpack_secret", "backpack_api_key", "helius_api_key", "alchemy_api_key", "quicknode_rpc_token")) {
    if (-not (Test-Path "secrets\$n")) { New-Item -ItemType File -Force "secrets\$n" | Out-Null }
}

# Mirror private_key.txt → private_key (no comment lines)
if (Test-Path "secrets\private_key.txt") {
    $lines = Get-Content "secrets\private_key.txt" | Where-Object { $_ -notmatch '^\s*#' -and $_.Trim() -ne '' }
    if ($lines) { $lines | Set-Content "secrets\private_key" }
}

Write-Host ""
Write-Host "Done. Edit secrets/*.txt - prod Ledger: leave private_key.txt empty."
