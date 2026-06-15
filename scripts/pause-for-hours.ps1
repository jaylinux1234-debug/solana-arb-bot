# Pause bot + terminal jobs for N hours, then restart monitor.
param(
    [double]$Hours = 5
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$resumeAt = (Get-Date).AddHours($Hours)
Write-Host "Pausing until $resumeAt ($Hours h)..."

# Stop trading container (keeps redis/grafana/prometheus up)
docker stop solana-arb-monitor 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "docker stop failed (Docker Desktop may be restarting). Retry: docker stop solana-arb-monitor"
}

# Stop common local watchers (ignore missing PIDs)
$patterns = @("enhanced_monitor.py", "logs-tail.mjs", "ledger-sign-bridge.mjs")
foreach ($pat in $patterns) {
    Get-CimInstance Win32_Process -Filter "Name='node.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$pat*" } |
        ForEach-Object {
            Write-Host "Stopping PID $($_.ProcessId): $pat"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

$stateDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$stateFile = Join-Path $stateDir "pause-until.txt"
$resumeAt.ToString("o") | Set-Content -Path $stateFile -Encoding utf8

$resumeScript = @"
Set-Location '$Root'
Write-Host "[`$(Get-Date -Format o)] Auto-resume after pause"
npm run compose:prod:restart:no-build 2>&1 | Tee-Object -FilePath '$Root\logs\pause-resume.log' -Append
Remove-Item '$stateFile' -ErrorAction SilentlyContinue
"@

$tempScript = Join-Path $env:TEMP "solana-arb-resume-$(Get-Random).ps1"
$resumeScript | Set-Content -Path $tempScript -Encoding utf8

$delaySec = [int]($Hours * 3600)
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-Command",
    "Start-Sleep -Seconds $delaySec; & '$tempScript'"
)

Write-Host "Monitor stopped. Auto-resume scheduled in $Hours hour(s)."
Write-Host "State file: $stateFile"
Write-Host "Manual resume now: npm run compose:prod:restart:no-build"
