# Restart v2 bot on Windows (host)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'src\.v2\.main' } |
    ForEach-Object {
        Write-Host "Stopping v2 PID $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Start-Sleep -Seconds 2
$env:PYTHONUTF8 = "1"
$v2HealthPort = "8001"
$envFile = Join-Path $root ".env"
if (Test-Path $envFile) {
    $match = Select-String -Path $envFile -Pattern '^\s*V2_HEALTH_PORT\s*=\s*(\d+)\s*$' | Select-Object -First 1
    if ($match -and $match.Matches.Groups[1].Value) {
        $v2HealthPort = $match.Matches.Groups[1].Value
    }
}
$env:BOT_HEALTH_PORT = $v2HealthPort
$env:V2_HEALTH_PORT = $v2HealthPort
Write-Host "Starting v2 (health :$env:BOT_HEALTH_PORT)..."
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "python"
$psi.Arguments = "-m src.v2.main"
$psi.WorkingDirectory = $root
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.EnvironmentVariables["PYTHONUTF8"] = "1"
$psi.EnvironmentVariables["BOT_HEALTH_PORT"] = $env:BOT_HEALTH_PORT
$proc = [System.Diagnostics.Process]::Start($psi)
Write-Host "v2 started PID $($proc.Id) (python -m src.v2.main)."
