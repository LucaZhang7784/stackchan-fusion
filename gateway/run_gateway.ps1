$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = Join-Path $root 'state'
New-Item -ItemType Directory -Force -Path $state | Out-Null
$port = 8010
$created = $false
$mutex = New-Object System.Threading.Mutex($false, 'Local\StackChanFusionGatewayLaunch', [ref]$created)
if (-not $mutex.WaitOne(10000)) { throw '网关启动锁超时，请稍后重试' }
try {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { Write-Host "网关已在运行 (PID $($listener[0].OwningProcess))"; exit 0 }
    $py = "C:\Users\zhang.luca\AppData\Local\Programs\Python\Python311\python.exe"
    if (-not (Test-Path $py)) { $py = (Get-Command python).Source }
    if (-not $py) { throw '未找到 python' }
    $p = Start-Process -FilePath $py -ArgumentList @('fusion_gateway.py','--transport','http','--host','0.0.0.0','--port',"$port") -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $state 'gateway.stdout.log') -RedirectStandardError (Join-Path $state 'gateway.stderr.log') -PassThru
    for ($i = 0; $i -lt 50; $i++) {
        $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener -and $listener.OwningProcess -eq $p.Id) { break }
        Start-Sleep -Milliseconds 100
    }
    if (-not $listener -or $listener.OwningProcess -ne $p.Id) {
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        throw '网关未在 5 秒内完成端口绑定'
    }
    Set-Content -LiteralPath (Join-Path $state 'gateway.pid') -Value $p.Id
    Write-Host "网关已启动 PID $($p.Id), 端口 $port"
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
