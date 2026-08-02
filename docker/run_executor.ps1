$ErrorActionPreference = "Continue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = (Get-Command python).Source
$existing = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*host-executor.py*" }
if ($existing) {
    Write-Host "host-executor already running pid=$($existing.ProcessId)"
    exit 0
}
$p = Start-Process -FilePath $py -ArgumentList @("-u", (Join-Path $dir "host-executor.py")) `
    -WorkingDirectory $dir -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $dir "executor.log") `
    -RedirectStandardError (Join-Path $dir "executor.err") -PassThru
Write-Host "host-executor started pid=$($p.Id) (127.0.0.1:8091)"
