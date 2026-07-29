$ErrorActionPreference = "Stop"

$listeners = Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue
if (-not $listeners) {
    Write-Host "No SPST-UEA Runtime listener found on port 8767."
    return
}

try {
    Invoke-RestMethod `
        -Method Post `
        -Uri "http://127.0.0.1:8767/api/project-context/shutdown" `
        -ContentType "application/json" `
        -Body "{}" `
        -TimeoutSec 5 | Out-Null
} catch {
    Write-Host "Project Context supervisor did not acknowledge graceful shutdown."
}

$listeners | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
    Stop-Process -Id $_ -Force
    Write-Host "Stopped SPST-UEA Runtime process $_."
}
