$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = Split-Path -Parent $scriptDir
$python = (Get-Command python -ErrorAction Stop).Source

Push-Location $scriptDir
try {
    $expectedCapture = & $python -m spst_runtime.runtime_release_identity capture `
        --repository-root $repositoryRoot | ConvertFrom-Json
} finally {
    Pop-Location
}
if ($LASTEXITCODE -ne 0 -or $expectedCapture.status -ne "ready") {
    throw "SPST-UEA Runtime repository identity capture failed: $($expectedCapture.reason)"
}

$existing = Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Validating existing SPST-UEA Runtime at http://127.0.0.1:8767/"
} else {
    $runtimeStateDir = Join-Path $repositoryRoot ".spst\runtime"
    New-Item -ItemType Directory -Force -Path $runtimeStateDir | Out-Null
    $env:SPST_COCKPIT_DB_PATH = Join-Path $runtimeStateDir "spst_cockpit.db"
    $env:SPST_RUNTIME_REPOSITORY_ROOT = $repositoryRoot
    Write-Host "Starting SPST-UEA Runtime at http://127.0.0.1:8767/"
    Start-Process -FilePath $python -ArgumentList @("-m", "spst_runtime.web", "--host", "127.0.0.1", "--port", "8767") -WorkingDirectory $scriptDir -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

$health = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8767/health" -TimeoutSec 5
if ($health.StatusCode -ne 200) {
    throw "SPST-UEA Runtime health check failed: $($health.StatusCode)"
}

try {
    $runtimeIdentity = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8767/api/runtime-identity" `
        -TimeoutSec 15
} catch {
    throw "SPST-UEA Runtime release identity endpoint is unavailable."
}
if ($runtimeIdentity.schema -ne "spst-runtime-release-identity-v1") {
    throw "SPST-UEA Runtime release identity schema is invalid."
}
if ($runtimeIdentity.status -ne "ready" -or -not $runtimeIdentity.current_match) {
    throw "SPST-UEA Runtime process is stale: $($runtimeIdentity.reason)"
}
$expectedIdentity = $expectedCapture.repository_identity.identity_sha256
if (
    $runtimeIdentity.startup_repository_identity.identity_sha256 -ne $expectedIdentity -or
    $runtimeIdentity.current_identity_sha256 -ne $expectedIdentity
) {
    throw "SPST-UEA Runtime belongs to a different repository identity."
}

$contextStatus = Invoke-RestMethod -Uri "http://127.0.0.1:8767/api/project-context/status" -TimeoutSec 5
if ($contextStatus.schema -ne "spst-project-context-supervisor-status-v1") {
    throw "SPST-UEA Project Context supervisor status is invalid."
}
$corpus = Join-Path $repositoryRoot ".spst\project-context\corpus.json"
if (Test-Path -LiteralPath $corpus) {
    $body = @{query = "repository evidence context"} | ConvertTo-Json -Compress
    $context = Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8767/api/project-context/query" `
        -ContentType "application/json" `
        -Body $body `
        -TimeoutSec 15
    if ($context.status -notin @("ready", "empty")) {
        throw "SPST-UEA Project Context supervisor warm-up failed: $($context.reason)"
    }
}

Write-Host "SPST-UEA Runtime is ready: http://127.0.0.1:8767/"
