$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$pidFile = Join-Path $root "bridge.pid"
if (Test-Path $pidFile) {
    $id = [int](Get-Content $pidFile | Select-Object -First 1)
    Get-Process -Id $id -ErrorAction SilentlyContinue | Stop-Process -Force
    Remove-Item $pidFile -Force
}

Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*xiaozhi-mcp*mcp_pipe.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Write-Host "Bridge stopped"
