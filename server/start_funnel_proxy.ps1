$ErrorActionPreference = "Stop"
$dir = "D:/PROJECT_ROOT\server"
$py = (Get-Command python).Source

$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*funnel_proxy.py*" }
if ($existing) {
    Write-Host "funnel_proxy already running pid=$($existing.ProcessId)"
    exit 0
}

$p = Start-Process -FilePath $py -ArgumentList @("-u", (Join-Path $dir "funnel_proxy.py")) `
    -WorkingDirectory $dir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $dir "funnel_proxy.log") `
    -RedirectStandardError (Join-Path $dir "funnel_proxy.err") -PassThru
Write-Host "funnel_proxy started pid=$($p.Id)"
