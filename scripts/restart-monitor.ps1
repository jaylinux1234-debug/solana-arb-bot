# Production Restart Wrapper (Windows) — same role as restart-monitor.sh
# Usage: npm run restart:monitor

$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$logDir = "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $logDir "restart_$timestamp.log"
$container = "solana-arb-monitor"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line
}

Log "=== SOLANA ARB MONITOR RESTART STARTED ==="

if (Get-Command npm -ErrorAction SilentlyContinue) {
    npm run sync:compose-env 2>$null
}

$composeArgs = @(
    "--env-file", "compose.env",
    "-f", "docker-compose.yml",
    "-f", "infra/compose/docker-compose.prod.yml",
    "-f", "infra/compose/docker-compose.prod.override.yml",
    "-f", "infra/compose/docker-compose.monitoring.yml"
)

$encFiles = @(
    "private_key.enc.yaml",
    "private_key_cex_dex.enc.yaml",
    "jupiter_api_key.enc.yaml",
    "openai_api_key.enc.yaml",
    "backpack_secret.enc.yaml",
    "helius_api_key.enc.yaml",
    "oneinch_api_key.txt.enc.yaml",
    "cow_api_key.txt.enc.yaml",
    "pagerduty_routing_key.txt.enc.yaml"
)
$missingEnc = $false
foreach ($enc in $encFiles) {
    if (-not (Test-Path "secrets/encrypted/$enc")) {
        $missingEnc = $true
        break
    }
}
if ($missingEnc -and (Test-Path "infra/compose/docker-compose.plaintext-secrets.yml")) {
    $composeArgs += "-f", "infra/compose/docker-compose.plaintext-secrets.yml"
    Log "Using plaintext secrets overlay (secrets/encrypted/*.enc.yaml missing)"
}

docker compose @composeArgs config --quiet
if ($LASTEXITCODE -ne 0) {
    Log "Compose validation failed"
    exit 1
}

Log "Clearing stale Redis singleton lock (if present)..."
node scripts/clear-singleton-lock.mjs 2>$null

Log "Restarting services..."
docker compose @composeArgs up --build -d --remove-orphans
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$maxWait = 120
$healthy = $false
for ($elapsed = 5; $elapsed -le $maxWait; $elapsed += 5) {
    $status = docker inspect -f '{{.State.Health.Status}}' $container 2>$null
    if ($status -eq "healthy") {
        Log "Monitor is healthy (${elapsed}s)"
        $healthy = $true
        break
    }
    Log "Waiting for health... (${elapsed}s, status=$status)"
    Start-Sleep -Seconds 5
}

if (-not $healthy) {
    Log "Monitor did not become healthy within ${maxWait}s"
    docker inspect -f '{{json .State.Health}}' $container 2>$null | Add-Content $logFile
    exit 1
}

Log "Restart completed successfully. Log: $logFile"
Log "Follow: npm run logs:tail  |  npm run compose:logs"
Log "Tail now: docker logs -f $container --tail 100"
