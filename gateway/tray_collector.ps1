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
$historyFile = Join-Path $root 'state\broadcast_history.jsonl'
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
                  startedAt = [string]$r.started_at; robotPresence = [string]$r.robot_presence;
                  robotPresenceUpdatedAt = $r.robot_presence_updated_at;
                  robotDiag = $r.robot_diag;
                  detail = "PID=$($r.pid) 启动=$($r.started_at) 工具=$(@($r.tools).Count) 个" }
    } catch {
        return @{ ok = $false; pid = 0; tools = 0; attached = $true; startedAt = ''; robotPresence = 'unknown'; robotPresenceUpdatedAt = 0; robotDiag = $null; detail = "连接失败: $($_.Exception.Message)" }
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
        $tail = Get-Content -LiteralPath $bridgeErr -Encoding UTF8 -Tail 200 -ErrorAction SilentlyContinue
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
        $pending = @(Get-Content -LiteralPath $pendingFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() }).Count
    }
    $events = @()
    if (Test-Path -LiteralPath $eventsFile) {
        $events = @(Get-Content -LiteralPath $eventsFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    }
    $confirm = 0
    if (Test-Path -LiteralPath $confirmFile) {
        try {
            $items = Get-Content -Raw -LiteralPath $confirmFile -Encoding UTF8 | ConvertFrom-Json
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

function Get-BroadcastHistory {
    # 读取播报历史。起播率/起播延迟只采集有 play_start 字段的新固件记录，
    # 绝不将旧的"整段推送处理耗时"混充为开始播报延迟。
    $hist = @()
    if (Test-Path -LiteralPath $historyFile) {
        $hist = @(Get-Content -LiteralPath $historyFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    }
    $last = ''
    $lastTs = ''
    $recent = @()
    $today = Get-Date -Format 'yyyy-MM-dd'
    $todayStarted = 0
    $startOutcomes = 0
    $startAcks = 0
    $startLatencies = @()
    if ($hist.Count -gt 0) {
        try {
            $o = $hist[-1] | ConvertFrom-Json
            $t = [string]$o.text; $t = $t -replace '\s+', ' '
            if ($t.Length -gt 40) { $t = $t.Substring(0, 40) + '...' }
            $last = "$($o.ts) [$($o.status)] $t"
            $lastTs = [string]$o.ts
        } catch { }
        $n = [Math]::Min(5, $hist.Count)
        for ($i = $hist.Count - $n; $i -lt $hist.Count; $i++) {
            try {
                $o = $hist[$i] | ConvertFrom-Json
                $t = [string]$o.text; $t = $t -replace '\s+', ' '
                if ($t.Length -gt 50) { $t = $t.Substring(0, 50) + '...' }
                $recent += "[$($o.ts)] [$($o.status)] $($o.source): $t"
            } catch { }
        }
        foreach ($line in $hist) {
            try {
                $o = $line | ConvertFrom-Json
                if (-not ([string]$o.ts).StartsWith($today)) { continue }
                $status = [string]$o.status
                # play_start = 固件已将首帧提交给音频输出驱动；缺失代表旧历史，不能纳入新指标。
                if ($null -ne $o.play_start) {
                    $startOutcomes++
                    if ([bool]$o.play_start) {
                        $startAcks++
                        $todayStarted++
                    }
                    if ($null -ne $o.start_latency_ms -and [double]$o.start_latency_ms -gt 0) {
                        $startLatencies += [double]$o.start_latency_ms
                    }
                }
            } catch { }
        }
    }
    $successRate = if ($startOutcomes -gt 0) { '{0}%' -f [math]::Round(($startAcks * 100.0) / $startOutcomes) } else { '待采样' }
    $avgLatency = if ($startLatencies.Count -gt 0) { '{0:N1}s' -f (($startLatencies | Measure-Object -Average).Average / 1000.0) } else { '待采样' }
    return $hist.Count, $last, $lastTs, $recent, $todayStarted, $successRate, $avgLatency
}

function Get-RobotPresence {
    param($gw)
    # 新固件有 retained MQTT status; 旧固件回退到最近 ACK, 兼容未刷机期间。
    if ($gw.robotPresence -eq 'online') { return @{ online = $true; state = 'online'; detail = '在线(MQTT 状态)' } }
    if ($gw.robotPresence -eq 'offline') { return @{ online = $false; state = 'offline'; detail = '离线(MQTT 状态)' } }
    try {
        if ($gw.startedAt -and ((Get-Date) - [datetime]::Parse($gw.startedAt)).TotalSeconds -lt 60) {
            return @{ online = $false; state = 'reconnecting'; detail = '启动/重连中(MQTT 状态待到达)' }
        }
    } catch { }
    $logFile = Join-Path $root 'gateway.log'
    if (Test-Path -LiteralPath $logFile) {
        $lines = Get-FileTail $logFile 262144 | Where-Object { $_ -match 'robot ping (ack|no-ack)|push (ack|no-ack)' }
        if ($lines.Count -gt 0) {
            if ($lines[-1] -notmatch 'no-ack') { return @{ online = $true; state = 'online'; detail = '在线(最近 ACK，等待 MQTT 状态)' } }
            return @{ online = $false; state = 'offline'; detail = '离线(最近探活/播报无 ACK)' }
        }
    }
    return @{ online = $false; state = 'reconnecting'; detail = '重连中(尚无 MQTT 状态或 ACK)' }
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
$script:gatewayFailureCount = 0
function Restore-GatewayIfDown([bool]$ok) {
    if ($ok) { return }
    if (((Get-Date) - $script:lastGwRestart).TotalSeconds -lt 30) { return }
    $script:lastGwRestart = Get-Date
    try { Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'run_gateway.ps1')`"") -WindowStyle Hidden | Out-Null } catch { }
}

function Write-StatusSnapshot([string]$json) {
    # 原子替换: 读端只能看到上一份完整快照或新快照，绝不读到半写入 JSON。
    $tmp = "$statusFile.$PID.tmp"
    $bak = "$statusFile.$PID.swap.bak"
    try {
        [System.IO.File]::WriteAllText($tmp, $json, $utf8)
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                if (Test-Path -LiteralPath $statusFile) {
                    # Windows PowerShell/.NET Framework 不接受 null 作为 Replace 的备份路径。
                    [System.IO.File]::Replace($tmp, $statusFile, $bak)
                } else {
                    [System.IO.File]::Move($tmp, $statusFile)
                }
                return
            } catch {
                if ($attempt -eq 3) { throw }
                Start-Sleep -Milliseconds 80
            }
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Force -ErrorAction SilentlyContinue }
    }
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
        if ($gw.ok) { $script:gatewayFailureCount = 0 } else { $script:gatewayFailureCount++ }
        # 一次本机 HTTP 短暂抖动只显示刷新中；连续两轮（约 10 秒）失败才标红及自愈。
        $gatewayConfirmedDown = ($script:gatewayFailureCount -ge 2)
        Restore-GatewayIfDown $gatewayConfirmedDown
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
        $hist = Get-BroadcastHistory
        $presence = Get-RobotPresence $gw
        $hookFault = Get-HookFault

        if ($gatewayConfirmedDown)                { $state = 'bad';  $label = '网关离线' }
        elseif (-not $gw.ok)                      { $state = 'warn'; $label = '网关状态刷新中' }
        elseif ($presence.state -eq 'reconnecting') { $state = 'warn'; $label = '机器人重连中' }
        elseif (-not $presence.online)            { $state = 'bad';  $label = '机器人离线' }
        elseif (-not $mcp -or $hookFault)         { $state = 'warn'; $label = '组件异常' }
        else                                      { $state = 'ok';   $label = '全部正常' }
        if (-not $gw.attached) { $label = '已断开(不推流)' }

        $diagText = ''
        if ($gw.robotDiag -and [string]$gw.robotDiag.reason) { $diagText = "`n【MQTT 诊断】$($gw.robotDiag.state): $($gw.robotDiag.reason), 离线 $($gw.robotDiag.offline_ms)ms" }
        $detail = "【Gateway】$($gw.detail)`n【MCP】$(if($mcp){'profile stackchan 正常'}else{'profile stackchan 异常'})`n【Hook 自检】$(if($hookFault){'存在异常'}else{'正常'})`n【Robot 本体】$($presence.detail)$diagText`n【Robot 桥】bridge=$($bridge.proc) 进程, 心跳=$($bridge.hb) 分钟, 最近推送: $lastPush`n【播报队列】待推送 $($queue.pending) 条, 待播报事件 $($queue.events) 条, 待确认 $($queue.confirm) 个, 合计 $($queue.total) 条`n【播报历史】$($hist[1])`n【连接】$(if($gw.attached){'已连接机器人'}else{'已断开: 消息入队不推流, 连接后自动补推'})"

        $effectiveGwOk = ($gw.ok -or -not $gatewayConfirmedDown)
        $status = @{
            ts = (Get-Date -Format 'HH:mm:ss'); state = $state; label = $label
            gwOk = $effectiveGwOk; mcpOk = $mcp; robotOk = $bridge.online; gwPid = $gw.pid; tools = $gw.tools
            pending = $queue.pending; events = $queue.events; confirm = $queue.confirm; total = $queue.total
            lastPush = $lastPush; lastCall = $bridge.lastCall; bridgeProc = $bridge.proc; hbMin = $bridge.hb
            attached = $gw.attached; detail = $detail; robotOnline = $presence.online; robotPresence = $presence.state; hookFault = $hookFault
            robotDiag = $gw.robotDiag
            histCount = $hist[0]; lastBroadcast = $hist[1]; lastBroadcastTs = $hist[2]; broadcastRecent = $hist[3]
            broadcastToday = $hist[4]; broadcastSuccessRate = $hist[5]; avgLatency = $hist[6]
        }
        Write-StatusSnapshot ($status | ConvertTo-Json -Compress)
    } catch {
        Write-Log "tray_collector 采集异常: $($_.Exception.Message)"
    }
    Start-Sleep -Seconds 5    # Widget/托盘实时状态刷新周期
}
