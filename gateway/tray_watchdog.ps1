$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = Join-Path $root 'state'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$log = Join-Path $state 'watchdog.log'

function Write-Log($msg) {
    Add-Content -LiteralPath $log -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg" -Encoding UTF8
}

# 幂等: fusion_tray.ps1 已在运行则不做任何事
$running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'fusion_tray\.ps1' }
if ($running) {
    exit 0
}

Write-Log "tray DOWN, restarting..."
& (Join-Path $root 'fusion_tray_hidden.vbs')
Start-Sleep -Seconds 3
$after = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'fusion_tray\.ps1' }
if ($after) {
    Write-Log "tray restarted OK (pid=$($after[0].ProcessId))"
} else {
    Write-Log "tray restart FAILED"
}
