$ErrorActionPreference = "Stop"

$listeners = Get-NetTCPConnection -LocalPort 8767 -State Listen -ErrorAction SilentlyContinue
if (-not $listeners) {
    Write-Host "No SPST-UEA Runtime listener found on port 8767."
    return
}

$listeners | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
    Stop-Process -Id $_ -Force
    Write-Host "Stopped SPST-UEA Runtime process $_."
}
