# Bootstrap: secrets/ + .env from template (does not overwrite existing .env).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

node scripts/init-secrets-templates.mjs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
node scripts/sync-secrets-local.mjs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env" -Force
    Write-Host "Created .env from .env.example"
} else {
    Write-Host ".env already exists (not overwritten)"
}

if (Test-Path ".env.txt") {
    Write-Host "WARN: .env.txt found - run: npm run secrets:migrate"
}

Write-Host "secrets/ ready - edit .env and run: npm run sync:compose-env"
