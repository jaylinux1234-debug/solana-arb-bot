# Static analysis before mainnet deploy: solhint + slither (Windows)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Write-Host "=== audit:all (PowerShell) ==="

Write-Host "[0/3] hardhat compile"
npx hardhat compile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "[1/3] solhint"
npx solhint "contracts/**/*.sol"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "solhint reported issues (review above)"
}

Write-Host "[2/3] slither"
if (Get-Command slither -ErrorAction SilentlyContinue) {
    slither . --exclude-dependencies
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "slither reported findings (review above)"
    }
} else {
    Write-Host "  slither not installed - pip install slither-analyzer"
}

Write-Host "=== audit:all complete ==="
