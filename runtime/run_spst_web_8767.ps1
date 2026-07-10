$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$existing = Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "SPST-UEA Runtime is already listening on http://127.0.0.1:8767/"
    return
}

$python = (Get-Command python -ErrorAction Stop).Source
Write-Host "Starting SPST-UEA Runtime at http://127.0.0.1:8767/"
Start-Process -FilePath $python -ArgumentList @("-m", "spst_runtime.web", "--host", "127.0.0.1", "--port", "8767") -WorkingDirectory $scriptDir -WindowStyle Hidden
Start-Sleep -Seconds 2

$health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8767/health" -TimeoutSec 5
if ($health.StatusCode -ne 200) {
    throw "SPST-UEA Runtime health check failed: $($health.StatusCode)"
}

Write-Host "SPST-UEA Runtime is ready: http://127.0.0.1:8767/"
