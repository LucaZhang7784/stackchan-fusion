$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*xiaozhi-mcp*mcp_pipe.py*" }
if ($existing) {
    Write-Host "Bridge already running pid=$($existing.ProcessId)"
    exit 0
}

$envFile = Join-Path $root ".env"
$line = Get-Content $envFile | Where-Object { $_ -like "MCP_ENDPOINT=*" } | Select-Object -First 1
if (-not $line) {
    Write-Host "MCP_ENDPOINT not found in .env"
    exit 1
}
$env:MCP_ENDPOINT = ($line -replace "^MCP_ENDPOINT=", "").Trim().Trim('"')

$python = "C:\Users\<USER>\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python).Source }
$pipe = Join-Path $root "mcp_pipe.py"
$server = Join-Path $root "server.py"
$log = Join-Path $root "bridge.log"
$errLog = Join-Path $root "bridge.err"
$pidFile = Join-Path $root "bridge.pid"

$p = Start-Process -FilePath $python -ArgumentList @("-u", "`"$pipe`"", "`"$server`"") `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput $log -RedirectStandardError $errLog -PassThru
$p.Id | Set-Content $pidFile
Write-Host "Bridge started pid=$($p.Id)"
Start-Sleep -Seconds 10
if (Test-Path $errLog) {
    Get-Content $errLog -Tail 25
}
