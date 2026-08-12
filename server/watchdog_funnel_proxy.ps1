$ErrorActionPreference = "SilentlyContinue"
$dir = "D:/PROJECT_ROOT\server"
$py = (Get-Command python).Source

$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*funnel_proxy.py*" }
if ($running) {
    exit 0
}

Start-Process -FilePath $py -ArgumentList @("-u", (Join-Path $dir "funnel_proxy.py")) `
    -WorkingDirectory $dir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $dir "funnel_proxy.log") `
    -RedirectStandardError (Join-Path $dir "funnel_proxy.err")
Add-Content -Path (Join-Path $dir "funnel_proxy_watchdog.log") -Value ("restarted at " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
