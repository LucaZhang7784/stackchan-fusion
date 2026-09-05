$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = Join-Path $root 'state'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$log = Join-Path $state 'watchdog.log'

function Write-Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Add-Content -LiteralPath $log -Value $line -Encoding UTF8
}

# 幂等检查: 8010 已在监听则什么都不做
$listener = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Log "gateway OK (pid=$($listener[0].OwningProcess))"
    exit 0
}

Write-Log "gateway DOWN, restarting..."
& (Join-Path $root 'run_gateway.ps1') *>> (Join-Path $state 'watchdog-run.log')

# 等 3 秒再确认一次
Start-Sleep -Seconds 3
$listener = Get-NetTCPConnection -LocalPort 8010 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    Write-Log "gateway restarted OK (pid=$($listener[0].OwningProcess))"
} else {
    Write-Log "gateway restart FAILED"
}
