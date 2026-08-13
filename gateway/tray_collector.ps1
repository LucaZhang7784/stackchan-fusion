# tray_collector.ps1 — 托盘后台状态采集器(独立进程)
# 由 fusion_tray.ps1 启动; 每 5s 采集一次状态写入 state\tray_status.json,
# 托盘 UI 只读该 JSON —— 彻底消除 UI 线程被 docker/WMI/HTTP 阻塞导致的卡顿。
# 退出托盘时由托盘按 PID 文件停止本进程。
param([string]$root = '')
if (-not $root) { $root = Split-Path -Parent $MyInvocation.MyCommand.Path }

$configPath = Join-Path $root 'config.json'
$gatewayUrl = 'http://127.0.0.1:8010'
$authToken = ''
try { $authToken = (Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json).auth_token } catch { }
$statusFile = Join-Path $root 'state\tray_status.json'
$pidFile    = Join-Path $root 'state\tray_collector.pid'
$bridgeErr  = Join-Path (Split-Path -Parent $root) 'xiaozhi-mcp\bridge.err'
$eventsFile = Join-Path $root 'data\agent_events.jsonl'
$confirmFile = Join-Path $root 'data\agent_confirmations.json'
$errLog     = Join-Path $root 'state\tray_err.log'

# 单实例保护
$existing = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'tray_collector\.ps1' -and $_.ProcessId -ne $PID }
if ($existing) { exit 0 }
try { Set-Content -LiteralPath $pidFile -Value $PID -NoNewline -ErrorAction Stop } catch { }

function Write-Log([string]$m) {
    try { Add-Content -LiteralPath $errLog -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m) -Encoding UTF8 } catch { }
}

function Get-FileTail {
    param([string]$path, [int]$maxBytes = 262144)
    try {
        $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
        try {
            if ($fs.Length -gt $maxBytes) { $fs.Seek(-$maxBytes, 'End') | Out-Null }
            $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
            $txt = $sr.ReadToEnd()
            $sr.Dispose()
            return @($txt -split "`r?`n" | Where-Object { $_ -match 'push (ack|ok|no-ack)|robot ping (ack|no-ack)' })
        } finally { $fs.Dispose() }
    } catch { return @() }
}

function Test-Gateway {
    try {
        $r = Invoke-RestMethod -Uri "$gatewayUrl/healthz" -Headers @{ Authorization = "Bearer $authToken" } -TimeoutSec 3
        return @{ ok = ($r.status -eq 'ok'); pid = $r.pid; tools = @($r.tools).Count;
                  attached = if ($null -ne $r.attached) { [bool]$r.attached } else { $true };
                  detail = "PID=$($r.pid) 启动=$($r.started_at) 工具=$(@($r.tools).Count) 个" }
    } catch {
        return @{ ok = $false; pid = 0; tools = 0; attached = $true; detail = "连接失败: $($_.Exception.Message)" }
    }
}

function Test-McpToolkit {
    try {
        $out = & docker mcp profile show stackchan 2>&1 | Out-String
        return ($out -match 'fusion-gateway')
    } catch { return $false }
}

function Get-BridgeInfo {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'mcp_pipe\.py|xiaozhi-mcp[\\/]server\.py' }
    $procCount = @($procs).Count
    $heartbeatMin = -1
    $lastCall = ''
    if (Test-Path -LiteralPath $bridgeErr) {
        $ageMin = ((Get-Date) - (Get-Item -LiteralPath $bridgeErr).LastWriteTime).TotalMinutes
        $heartbeatMin = [math]::Round($ageMin, 1)
        $tail = Get-Content -LiteralPath $bridgeErr -Tail 200 -ErrorAction SilentlyContinue
        $callLine = $tail | Where-Object { $_ -match 'CallToolRequest' } | Select-Object -Last 1
        if ($callLine -and $callLine -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
            try {
                $minsAgo = [math]::Round(((Get-Date) - [datetime]::ParseExact($matches[1], 'yyyy-MM-dd HH:mm:ss', $null)).TotalMinutes, 1)
                $lastCall = "$($matches[1]) (${minsAgo} 分钟前)"
            } catch { $lastCall = $matches[1] }
        }
    }
    $online = ($procCount -ge 1 -and $heartbeatMin -ge 0 -and $heartbeatMin -le 3)
    return @{ proc = $procCount; hb = $heartbeatMin; online = $online; lastCall = $lastCall }
}

function Get-QueueInfo {
    $pending = 0
    $pendingFile = Join-Path $root 'state\pending.jsonl'
    if (Test-Path -LiteralPath $pendingFile) {
        $pending = @(Get-Content -LiteralPath $pendingFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() }).Count
    }
    $events = @()
    if (Test-Path -LiteralPath $eventsFile) {
        $events = @(Get-Content -LiteralPath $eventsFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    }
    $confirm = 0
    if (Test-Path -LiteralPath $confirmFile) {
        try {
            $items = Get-Content -Raw -LiteralPath $confirmFile | ConvertFrom-Json
            $confirm = @($items | Where-Object { -not $_.answered }).Count
        } catch { }
    }
    return @{ pending = $pending; events = $events.Count; confirm = $confirm; total = ($pending + $events.Count + $confirm) }
}

function Get-CloudRobot {
    $lastPush = ''
    $logFile = Join-Path $root 'gateway.log'
    if (Test-Path -LiteralPath $logFile) {
        $line = Get-FileTail $logFile 262144 | Where-Object { $_ -match 'push (ack|ok)' } | Select-Object -Last 1
        if ($line -and $line -match '^\[([^\]]+)\]') {
            try {
                $ts = [datetime]::Parse($matches[1])
                $mins = [math]::Round(((Get-Date) - $ts).TotalMinutes, 1)
                $lastPush = "$($matches[1]) (${mins} 分钟前)"
            } catch { $lastPush = $matches[1] }
        }
    }
    return $lastPush
}

function Get-RobotOnline {
    # 机器人本体在线判定: 最近一次 探活/播报 结果(ack=在线, no-ack=离线); 无记录默认在线
    $logFile = Join-Path $root 'gateway.log'
    if (Test-Path -LiteralPath $logFile) {
        $lines = Get-FileTail $logFile 262144 | Where-Object { $_ -match 'robot ping (ack|no-ack)|push (ack|no-ack)' }
        if ($lines.Count -gt 0) {
            return ($lines[-1] -notmatch 'no-ack')
        }
    }
    return $true
}

function Get-HookFault {
    # Hook 自检结果: 最近一次 hook_health 输出含"存在异常" => 故障
    $f = Join-Path $root 'state\hook_health.last.txt'
    if (Test-Path -LiteralPath $f) {
        try {
            $t = Get-Content -Raw -LiteralPath $f -Encoding UTF8 -ErrorAction Stop
            if ($t -match '存在异常') { return $true }
        } catch { }
    }
    return $false
}

# 托盘内置守护(防抖 30s): 网关/云桥离线时静默拉起
$script:lastGwRestart = [DateTime]::MinValue
$script:lastBridgeRestart = [DateTime]::MinValue
function Restore-GatewayIfDown([bool]$ok) {
    if ($ok) { return }
    if (((Get-Date) - $script:lastGwRestart).TotalSeconds -lt 30) { return }
    $script:lastGwRestart = Get-Date
    try { Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'run_gateway.ps1')`"") -WindowStyle Hidden | Out-Null } catch { }
}
function Restore-BridgeIfDown([bool]$ok) {
    if ($ok) { return }
    if (((Get-Date) - $script:lastBridgeRestart).TotalSeconds -lt 30) { return }
    $script:lastBridgeRestart = Get-Date
    try { Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path (Split-Path -Parent $root) 'xiaozhi-mcp\run_bridge.ps1')`"") -WindowStyle Hidden | Out-Null } catch { }
}

$script:lastMcpCheck = 0
$script:lastMcpOk = $false
$utf8 = New-Object System.Text.UTF8Encoding($false)
Write-Log "tray_collector 启动 PID=$PID"

while ($true) {
    try {
        $gw = Test-Gateway
        Restore-GatewayIfDown $gw.ok
        $now = [Environment]::TickCount
        if ($script:lastMcpCheck -eq 0 -or ($now - $script:lastMcpCheck) -ge 30000) {
            $script:lastMcpCheck = $now
            $script:lastMcpOk = Test-McpToolkit
        }
        $mcp = $script:lastMcpOk
        $bridge = Get-BridgeInfo
        Restore-BridgeIfDown $bridge.online
        $queue = Get-QueueInfo
        $lastPush = Get-CloudRobot
        $robotOnline = Get-RobotOnline
        $hookFault = Get-HookFault

        if (-not $gw.ok)                          { $state = 'bad';  $label = '网关离线' }
        elseif (-not $robotOnline)                { $state = 'bad';  $label = '机器人离线' }
        elseif (-not $mcp -or $hookFault)         { $state = 'warn'; $label = '组件异常' }
        else                                      { $state = 'ok';   $label = '全部正常' }
        if (-not $gw.attached) { $label = '已断开(不推流)' }

        $detail = "【Gateway】$($gw.detail)`n【MCP】$(if($mcp){'profile stackchan 正常'}else{'profile stackchan 异常'})`n【Hook 自检】$(if($hookFault){'存在异常'}else{'正常'})`n【Robot 本体】$(if($robotOnline){'在线'}else{'离线(最近探活/播报无 ACK)'})`n【Robot 桥】bridge=$($bridge.proc) 进程, 心跳=$($bridge.hb) 分钟, 最近推送: $lastPush`n【播报队列】待推送 $($queue.pending) 条, 待播报事件 $($queue.events) 条, 待确认 $($queue.confirm) 个, 合计 $($queue.total) 条`n【连接】$(if($gw.attached){'已连接机器人'}else{'已断开: 消息入队不推流, 连接后自动补推'})"

        $status = @{
            ts = (Get-Date -Format 'HH:mm:ss'); state = $state; label = $label
            gwOk = $gw.ok; mcpOk = $mcp; robotOk = $bridge.online; gwPid = $gw.pid; tools = $gw.tools
            pending = $queue.pending; events = $queue.events; confirm = $queue.confirm; total = $queue.total
            lastPush = $lastPush; lastCall = $bridge.lastCall; bridgeProc = $bridge.proc; hbMin = $bridge.hb
            attached = $gw.attached; detail = $detail; robotOnline = $robotOnline; hookFault = $hookFault
        }
        [System.IO.File]::WriteAllText($statusFile, ($status | ConvertTo-Json -Compress), $utf8)
    } catch {
        Write-Log "tray_collector 采集异常: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 5
}
