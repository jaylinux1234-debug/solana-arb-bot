# Phase 3: audit -> Base deploy -> Basescan verify (Windows)
# Uses DEPLOYER_PRIVATE_KEY (Base EVM only — not the Solana trading key).
param([string]$Network = "base-mainnet")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Import-DotEnvFile($path) {
    if (-not (Test-Path $path)) { return }
    Get-Content $path | ForEach-Object {
        if ($_ -match '^\s*([^#=]+)=(.*)$') {
            Set-Item -Path "env:$($matches[1].Trim())" -Value $matches[2].Trim()
        }
    }
}

Import-DotEnvFile ".env"
Import-DotEnvFile "compose.env"
if (Test-Path ".env.txt") {
    Write-Error ".env.txt is deprecated — run: npm run secrets:migrate"
}

if ($env:PRIVATE_KEY -or $env:PRIVATE_KEY_CEX_DEX) {
    Write-Error "Unset PRIVATE_KEY / PRIVATE_KEY_CEX_DEX before deploy:secure (use DEPLOYER_PRIVATE_KEY for Base only)."
}

$deployer = $env:DEPLOYER_PRIVATE_KEY
if (-not $deployer) { $deployer = $env:BASE_DEPLOYER_PRIVATE_KEY }
if (-not $deployer) {
    Write-Error "Set DEPLOYER_PRIVATE_KEY for Base contract deploy (EVM key, not Solana PRIVATE_KEY)."
}

$owner = $env:GNOSIS_SAFE_ADDRESS
if (-not $owner) { $owner = $env:TIMELOCK_ADDRESS }
if (-not $owner) {
    Write-Error "Set GNOSIS_SAFE_ADDRESS (preferred) or TIMELOCK_ADDRESS as contract owner."
}

if (-not (Test-Path node_modules)) { npm install }

Write-Host "=== deploy:secure network=$Network owner=$owner ==="
npm run audit:all:ps1
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$verify = @()
if ($env:BASESCAN_API_KEY -or $env:ETHERSCAN_API_KEY) {
    $verify = @("--verify")
    Write-Host "  Basescan verify enabled"
} else {
    Write-Warning "Set BASESCAN_API_KEY to auto-verify on deploy"
}

npx hardhat ignition deploy ignition/modules/ArbMonitorRegistry.ts --network $Network @verify
exit $LASTEXITCODE
