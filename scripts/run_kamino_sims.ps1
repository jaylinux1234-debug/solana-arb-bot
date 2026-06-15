# Batch Kamino collateral simulations for wallet_safety (1000+ target).
# Requires .env with SOLANA_RPC_URL, PRIVATE_KEY, PRIVATE_KEY_CEX_DEX, Kamino reserve pubkeys.
#
# Usage (from repo root):
#   .\scripts\run_kamino_sims.ps1
#   .\scripts\run_kamino_sims.ps1 -PerSigner 200 -Rounds 5   # 2000 total OK loops max

param(
    [int]$PerSigner = 200,
    [int]$Rounds = 1
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Python = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating venv..."
    python -m venv venv
    & (Join-Path $Root "venv\Scripts\pip.exe") install -r requirements.txt
}

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Write-Host "ERROR: .env missing. Run: Copy-Item .env.example .env" -ForegroundColor Red
    exit 2
}

$env:TEST_MODE = "true"
for ($r = 1; $r -le $Rounds; $r++) {
    Write-Host "=== Round $r / $Rounds | primary signer | $PerSigner sims ===" -ForegroundColor Cyan
    & $Python cex_dex_sim_batch.py --count $PerSigner --mode kamino_collateral --signer primary
    if ($LASTEXITCODE -ne 0) { Write-Host "primary batch finished with exit $LASTEXITCODE (partial OK is normal)" -ForegroundColor Yellow }

    Write-Host "=== Round $r / $Rounds | cex signer | $PerSigner sims ===" -ForegroundColor Cyan
    & $Python cex_dex_sim_batch.py --count $PerSigner --mode kamino_collateral --signer cex
    if ($LASTEXITCODE -ne 0) { Write-Host "cex batch finished with exit $LASTEXITCODE (partial OK is normal)" -ForegroundColor Yellow }
}

Write-Host "Done. Check logs above for successful_sim_count delta." -ForegroundColor Green
