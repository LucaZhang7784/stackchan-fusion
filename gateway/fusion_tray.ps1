$ErrorActionPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# 单实例保护: 已有托盘在跑(排除自身)则直接退出, 避免双图标
$existingTray = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
    Where-Object { $_.CommandLine -match '(?i)-File\s+"?[^"]*fusion_tray\.ps1' -and $_.ProcessId -ne $PID }
if ($existingTray) { exit 0 }

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $root 'config.json'
$gatewayUrl = 'http://127.0.0.1:8010'
$authToken = ''
try {
    $cfg = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    $authToken = $cfg.auth_token
} catch { }
if (-not $authToken) { $authToken = '' }

$bridgeErr = Join-Path (Split-Path -Parent $root) 'xiaozhi-mcp\bridge.err'
$eventsFile = Join-Path $root 'data\agent_events.jsonl'
$confirmFile = Join-Path $root 'data\agent_confirmations.json'
$historyFile = Join-Path $root 'state\broadcast_history.jsonl'

$script:lastState = ''              # 上一次总状态
$script:lastRestartAt = [DateTime]::MinValue
$script:lastBridgeRestartAt = [DateTime]::MinValue
$script:gwOk = $false
$script:mcpOk = $false
$script:robotOk = $false
$script:tools = @()
$script:gwPid = 0
$script:detail = ''
$script:errLog = Join-Path $root 'state\tray_err.log'

function Write-TrayLog {
    param([string]$msg)
    try {
        Add-Content -LiteralPath $script:errLog -Value ("[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) -Encoding UTF8
    } catch { }
}

# ---------------------------------------------------------------- 状态采集
function Test-Gateway {
    param([ref]$detailRef)
    try {
        $r = Invoke-RestMethod -Uri "$gatewayUrl/healthz" -Headers @{ Authorization = "Bearer $authToken" } -TimeoutSec 3
        $script:gwOk = ($r.status -eq 'ok')
        $script:tools = @($r.tools)
        $script:gwPid = $r.pid
        $script:attached = if ($null -ne $r.attached) { [bool]$r.attached } else { $true }
        $detailRef.Value = "PID=$($r.pid) 启动=$($r.started_at) 工具=$($script:tools.Count) 个"
        return $script:gwOk
    } catch {
        $script:gwOk = $false
        $script:tools = @()
        $script:gwPid = 0
        $script:attached = $true
        $detailRef.Value = "连接失败: $($_.Exception.Message)"
        return $false
    }
}

function Test-McpToolkit {
    param([ref]$detailRef)
    try {
        $out = & docker mcp profile show stackchan 2>&1 | Out-String
        $script:mcpOk = ($out -match 'fusion-gateway')
        if ($script:mcpOk) { $detailRef.Value = "profile 'stackchan' 正常" }
        else { $detailRef.Value = "profile 'stackchan' 异常" }
        return $script:mcpOk
    } catch {
        $script:mcpOk = $false
        $detailRef.Value = "检查失败: $($_.Exception.Message)"
        return $false
    }
}

function Get-BridgeInfo {
    # 返回 (进程数, 心跳分钟, 是否在线, 最近工具调用)
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'mcp_pipe\.py|xiaozhi-mcp[\\/]server\.py' }
    $procCount = @($procs).Count
    $heartbeatMin = -1
    $lastCall = ''
    if (Test-Path -LiteralPath $bridgeErr) {
        $ageMin = ((Get-Date) - (Get-Item -LiteralPath $bridgeErr).LastWriteTime).TotalMinutes
        $heartbeatMin = [math]::Round($ageMin, 1)
        # 最近一次工具调用时间
        $tail = Get-Content -LiteralPath $bridgeErr -Encoding UTF8 -Tail 200 -ErrorAction SilentlyContinue
        $callLine = $tail | Where-Object { $_ -match 'CallToolRequest' } | Select-Object -Last 1
        if ($callLine) {
            if ($callLine -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})') {
                $ts = $matches[1]
                try {
                    $minsAgo = [math]::Round(((Get-Date) - [datetime]::ParseExact($ts, 'yyyy-MM-dd HH:mm:ss', $null)).TotalMinutes, 1)
                    $lastCall = "$ts (${minsAgo} 分钟前)"
                } catch { $lastCall = $ts }
            }
        }
    }
    $online = ($procCount -ge 1 -and $heartbeatMin -ge 0 -and $heartbeatMin -le 3)
    return @($procCount, $heartbeatMin, $online, $lastCall)
}

function Get-CloudRobot {
    # 云链路播报可达性: 以网关最近一次 push ack 时间作为机器人播报链路健康代理。
    # 自建服务器(8003)已停用(容器 Exited), 播报走 EMQX MQTT µ-law 推送。
    $lastPush = ''
    $logFile = Join-Path $root 'gateway.log'
    if (Test-Path -LiteralPath $logFile) {
        # 大日志尾部读取: 文件流 seek 末尾 256KB, 不整文件 Get-Content(23MB+ 会卡)
        $line = Get-FileTail $logFile 262144 |
            Where-Object { $_ -match 'push (ack|ok)' } | Select-Object -Last 1
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

function Get-FileTail {
    param([string]$path, [int]$maxBytes = 262144)
    try {
        $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
        try {
            if ($fs.Length -gt $maxBytes) { $fs.Seek(-$maxBytes, 'End') | Out-Null }
            $sr = New-Object System.IO.StreamReader($fs, [System.Text.Encoding]::UTF8)
            $txt = $sr.ReadToEnd()
            $sr.Dispose()
            return @($txt -split "`r?`n" | Where-Object { $_ -match 'push (ack|ok|no-ack)' })
        } finally { $fs.Dispose() }
    } catch { return @() }
}

# ---------------------------------------------------------------- 播报队列统计
# 当前架构: state/pending.jsonl = 待推送播报队列(单 Worker 串行推流, robot_say/桥接写入);
# agent_events = done/error 事件(由 _drain_pending 转入推送队列); confirmations = 待确认问题。
function Get-QueueInfo {
    # 返回 (待推送数, 事件数, 待确认数, 合计, 最近事件摘要)
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
    $summary = ''
    if ($events.Count -gt 0) {
        try {
            $o = $events[-1] | ConvertFrom-Json
            $s = [string]$o.summary
            $s = $s -replace '\[([^\]]+)\]\([^)]*\)', '$1'
            $s = $s -replace '[`*_#>{}]', ' '
            $s = $s -replace 'file:///\S*', ''
            $s = $s -replace '\b[A-Za-z]:[\\/][^\s，。；、)]*', ''
            $s = $s -replace '\s+', ' '
            $summary = "[$($o.agent) $($o.type)] $($s.Trim())"
            if ($summary.Length -gt 60) { $summary = $summary.Substring(0, 60) + '...' }
        } catch { }
    }
    return @($pending, $events.Count, $confirm, ($pending + $events.Count + $confirm), $summary)
}

# ---------------------------------------------------------------- 队列操作
function Get-QueueItems {
    # 返回 (事件条目[], 推送条目[])
    $evs = @()
    if (Test-Path -LiteralPath $eventsFile) {
        $evs = @(Get-Content -LiteralPath $eventsFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() } | ForEach-Object {
            try { $o = $_ | ConvertFrom-Json; "[$($o.ts)] $($o.agent) $($o.type): $($o.summary)" } catch { $_ }
        })
    }
    $pus = @()
    $pendingFile = Join-Path $root 'state\pending.jsonl'
    if (Test-Path -LiteralPath $pendingFile) {
        $pus = @(Get-Content -LiteralPath $pendingFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() } | ForEach-Object {
            try { $o = $_ | ConvertFrom-Json; "[$($o.created_at)] $($o.text)" } catch { $_ }
        })
    }
    return ,$evs, $pus
}

function Show-QueueMessages {
    $lines = @()
    $evs = @()
    if (Test-Path -LiteralPath $eventsFile) {
        $evs = @(Get-Content -LiteralPath $eventsFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    }
    if ($evs.Count) {
        $lines += '===== 待播报事件 (agent_events) ====='
        foreach ($e in $evs) {
            try {
                $o = $e | ConvertFrom-Json
                $s = [string]$o.summary
                $s = $s -replace '\s+', ' '
                if ($s.Length -gt 180) { $s = $s.Substring(0, 180) + '...' }
                $lines += "[$($o.ts)] $($o.agent) $($o.type): $s"
            } catch { $lines += $e }
        }
    }
    $pendingFile = Join-Path $root 'state\pending.jsonl'
    $pus = @()
    if (Test-Path -LiteralPath $pendingFile) {
        $pus = @(Get-Content -LiteralPath $pendingFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    }
    if ($pus.Count) {
        if ($lines.Count) { $lines += '' }
        $lines += '===== 推送队列 (待播报) ====='
        foreach ($p in $pus) {
            try {
                $o = $p | ConvertFrom-Json
                $t = [string]$o.text
                $t = $t -replace '\s+', ' '
                if ($t.Length -gt 180) { $t = $t.Substring(0, 180) + '...' }
                $lines += "[$($o.created_at)] $t"
            } catch { $lines += $p }
        }
    }
    if (-not $lines.Count) { $lines = '(队列为空)' }
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'StackChan 队列消息'
    $form.Size = New-Object System.Drawing.Size(720, 500)
    $form.StartPosition = 'CenterScreen'
    $form.TopMost = $true
    $txt = New-Object System.Windows.Forms.TextBox
    $txt.Multiline = $true
    $txt.ReadOnly = $true
    $txt.ScrollBars = 'Vertical'
    $txt.Dock = 'Fill'
    $txt.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9)
    $txt.Text = ($lines -join "`r`n")
    $form.Controls.Add($txt)
    $form.ShowDialog() | Out-Null
}

function Show-BroadcastHistory {
    # 播报历史队列: state/broadcast_history.jsonl(由网关 _push_worker 记录 ack/no-ack/fail), 最近 50 条倒序展示
    $lines = @()
    $hist = @()
    if (Test-Path -LiteralPath $historyFile) {
        $hist = @(Get-Content -LiteralPath $historyFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    }
    if ($hist.Count) {
        $from = [Math]::Max(0, $hist.Count - 50)
        for ($i = $hist.Count - 1; $i -ge $from; $i--) {
            try {
                $o = $hist[$i] | ConvertFrom-Json
                $t = [string]$o.text
                $t = $t -replace '\s+', ' '
                if ($t.Length -gt 200) { $t = $t.Substring(0, 200) + '...' }
                $statusLabel = switch ([string]$o.status) { 'ack' { '已播报' } 'no-ack' { '未确认' } 'fail' { '失败' } default { $o.status } }
                $lines += "[$($o.ts)] [$statusLabel] $($o.source): $t"
            } catch { $lines += $hist[$i] }
        }
    }
    if (-not $lines.Count) { $lines = '(暂无播报历史)' }
    $form = New-Object System.Windows.Forms.Form
    $form.Text = 'StackChan 播报历史 (最近 50 条)'
    $form.Size = New-Object System.Drawing.Size(760, 520)
    $form.StartPosition = 'CenterScreen'
    $form.TopMost = $true
    $txt = New-Object System.Windows.Forms.TextBox
    $txt.Multiline = $true
    $txt.ReadOnly = $true
    $txt.ScrollBars = 'Vertical'
    $txt.Dock = 'Fill'
    $txt.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9)
    $txt.Text = ($lines -join "`r`n")
    $form.Controls.Add($txt)
    $form.ShowDialog() | Out-Null
}

function Clear-QueueData {
    # 清空事件队列 + 推送队列(无 BOM 写入, 避免破坏 JSONL); 待确认问题保留。
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = Join-Path $root "state\queue-clear-$stamp.jsonl"
    $all = @()
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    if (Test-Path -LiteralPath $eventsFile) {
        $all += Get-Content -LiteralPath $eventsFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() }
        [System.IO.File]::WriteAllText($eventsFile, '', $utf8)
    }
    $pendingFile = Join-Path $root 'state\pending.jsonl'
    if (Test-Path -LiteralPath $pendingFile) {
        $all += Get-Content -LiteralPath $pendingFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() }
        [System.IO.File]::WriteAllText($pendingFile, '', $utf8)
    }
    if ($all.Count) { [System.IO.File]::WriteAllLines($backup, $all, $utf8) }
    [System.Windows.Forms.MessageBox]::Show(
        "已清空队列($($all.Count) 条)。备份: $(Split-Path $backup -Leaf)",
        '清空队列', 'OK', 'Information') | Out-Null
}

# 托盘内置守护: 网关离线时静默拉起(防抖 30s)
function Restore-GatewayIfDown {
    param([bool]$gatewayOk)
    if ($gatewayOk) { return }
    if (((Get-Date) - $script:lastRestartAt).TotalSeconds -lt 30) { return }
    $script:lastRestartAt = Get-Date
    try {
        $run = Join-Path $root 'run_gateway.ps1'
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$run`"") `
            -WindowStyle Hidden | Out-Null
    } catch {
        try { Add-Content -LiteralPath (Join-Path $root 'state' 'watchdog.log') -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] tray auto-restart error: $($_.Exception.Message)" -Encoding UTF8 } catch { }
    }
}

# 托盘内置守护: 云桥接挂了(进程<2 或心跳>3分钟)时静默拉起(防抖 30s)
function Restore-BridgeIfDown {
    param([bool]$bridgeOk)
    if ($bridgeOk) { return }
    if (((Get-Date) - $script:lastBridgeRestartAt).TotalSeconds -lt 30) { return }
    $script:lastBridgeRestartAt = Get-Date
    try {
        $run = Join-Path (Split-Path -Parent $root) 'xiaozhi-mcp\run_bridge.ps1'
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$run`"") `
            -WindowStyle Hidden | Out-Null
    } catch {
        try { Add-Content -LiteralPath (Join-Path $root 'state' 'watchdog.log') -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] tray bridge restart error: $($_.Exception.Message)" -Encoding UTF8 } catch { }
    }
}

# ---------------------------------------------------------------- 图标
$script:iconCache = @{}
function Get-StatusIcon {
    param([string]$state, [bool]$gw, [bool]$mcp, [bool]$robot)
    $key = "$state|$gw|$mcp|$robot"
    if ($script:iconCache.ContainsKey($key)) { return $script:iconCache[$key] }
    try {
        $bmp = New-Object System.Drawing.Bitmap 32, 32
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.Clear([System.Drawing.Color]::Transparent)

        # 健康色渐变背景
        switch ($state) {
            'ok'   { $c1 = [System.Drawing.Color]::FromArgb(46, 204, 113); $c2 = [System.Drawing.Color]::FromArgb(22, 160, 133); break }
            'warn' { $c1 = [System.Drawing.Color]::FromArgb(243, 156, 18); $c2 = [System.Drawing.Color]::FromArgb(211, 84, 0);  break }
            default{ $c1 = [System.Drawing.Color]::FromArgb(231, 76, 60);  $c2 = [System.Drawing.Color]::FromArgb(192, 57, 43); break }
        }
        $bgRect = New-Object System.Drawing.Rectangle 3, 8, 26, 21
        $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush $bgRect, $c1, $c2, 45
        $path = New-Object System.Drawing.Drawing2D.GraphicsPath
        $d = 8
        $path.AddArc(3, 8, $d, $d, 180, 90)
        $path.AddArc(29 - $d, 8, $d, $d, 270, 90)
        $path.AddArc(29 - $d, 29 - $d, $d, $d, 0, 90)
        $path.AddArc(3, 29 - $d, $d, $d, 90, 90)
        $path.CloseFigure()
        $g.FillPath($brush, $path)

        # 机器人样式: 圆角头部 + 天线 + 两只眼睛 + 嘴巴
        # 状态映射: 左眼=网关, 右眼=MCP, 天线灯=机器人
        $dotBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
        $dotOff   = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(120, 255, 255, 255))

        # 天线杆 + 天线灯(机器人状态)
        $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(230, 255, 255, 255)), 2
        $g.DrawLine($pen, 16, 8, 16, 3)
        $antBrush = if ($robot) { $dotBrush } else { $dotOff }
        $g.FillEllipse($antBrush, 13, 1, 6, 6)

        # 左眼 = 网关, 右眼 = MCP
        $eyeL = if ($gw)  { $dotBrush } else { $dotOff }
        $g.FillEllipse($eyeL, 7, 14, 7, 7)
        $eyeR = if ($mcp) { $dotBrush } else { $dotOff }
        $g.FillEllipse($eyeR, 18, 14, 7, 7)

        # 嘴巴(音箱格栅)
        $g.FillRectangle($dotBrush, 11, 24, 10, 2)
        $g.Dispose()
        $hicon = $bmp.GetHicon()
        $icon = [System.Drawing.Icon]::FromHandle($hicon)
        $bmp.Dispose()
        # 图标按状态缓存: 避免每 5 秒生成导致 GDI 句柄泄漏(GetHicon GDI+ 错误)
        $script:iconCache[$key] = $icon
        return $icon
    } catch {
        Write-TrayLog "Get-StatusIcon 生成失败, 降级缓存: $($_.Exception.Message)"
        if ($script:iconCache.Count -gt 0) {
            return $script:iconCache.Values | Select-Object -First 1
        }
        return $null
    }
}

# ---------------------------------------------------------------- 后台状态采集(不阻塞 UI)
$script:status = @{
    state = 'warn'; label = '采集初始中...'; gwOk = $false; mcpOk = $false; robotOk = $false
    gwPid = 0; tools = 0; pending = 0; events = 0; confirm = 0; total = 0
    lastPush = ''; lastCall = ''; bridgeProc = 0; hbMin = -1; attached = $true; detail = ''
    histCount = 0; lastBroadcast = ''; lastBroadcastTs = ''; broadcastRecent = @()
}
$script:attached = $true
$script:lastMcpCheck = 0
$script:lastMcpOk = $false
$script:collecting = $false

function Update-StatusCache {
    # 线程池线程运行: 采集全部状态写入 $script:status(整表替换, 原子引用), UI 只读缓存
    if ($script:collecting) { return }
    $script:collecting = $true
    try {
        $gwDetail = ''; $mcpDetail = ''
        $g = Test-Gateway -detailRef ([ref]$gwDetail)
        Restore-GatewayIfDown $g
        # docker mcp profile 探测较重, 30s 缓存一次
        $now = [Environment]::TickCount
        if ($script:lastMcpCheck -eq 0 -or ($now - $script:lastMcpCheck) -ge 30000) {
            $script:lastMcpCheck = $now
            $m = Test-McpToolkit -detailRef ([ref]$mcpDetail)
            $script:lastMcpOk = $m
        } else {
            $m = $script:lastMcpOk
            $mcpDetail = if ($m) { "profile 'stackchan' 正常(缓存)" } else { "profile 'stackchan' 异常(缓存)" }
        }
        $bridge = Get-BridgeInfo
        $r = $bridge[2]
        Restore-BridgeIfDown $r
        $queue = Get-QueueInfo
        $lastPush = Get-CloudRobot
        $attached = $script:attached

        if ($g -and $m -and $r) { $state = 'ok';   $label = '全部正常' }
        elseif ($g)             { $state = 'warn'; $label = '部分异常' }
        else                    { $state = 'bad';  $label = '网关离线' }
        if (-not $attached) { $label = '已断开(不推流)' }

        $detail = "【Gateway】$gwDetail`n【MCP】$mcpDetail`n【Robot】bridge=$($bridge[0]) 进程, 心跳=$($bridge[1]) 分钟, 最近推送: $lastPush`n【播报队列】待推送 $($queue[0]) 条, 待播报事件 $($queue[1]) 条, 待确认 $($queue[2]) 个, 合计 $($queue[3]) 条`n【连接】$(if($attached){'已连接机器人'}else{'已断开: 消息入队不推流, 连接后自动补推'})"

        $script:status = @{
            state = $state; label = $label; gwOk = $g; mcpOk = $m; robotOk = $r
            gwPid = $script:gwPid; tools = $script:tools.Count
            pending = $queue[0]; events = $queue[1]; confirm = $queue[2]; total = $queue[3]
            lastPush = $lastPush; lastCall = $bridge[3]; bridgeProc = $bridge[0]; hbMin = $bridge[1]
            attached = $attached; detail = $detail
        }
    } catch {
        Write-TrayLog "Update-StatusCache 异常: $($_.Exception.Message)"
    } finally {
        $script:collecting = $false
    }
}

# ---------------------------------------------------------------- 后台任务(慢操作不冻结 UI)
$script:busyJob = $null
$script:busyDone = $null

function Start-BusyJob {
    param([string]$title, [scriptblock]$work, [scriptblock]$done, [object[]]$argsList = @())
    if ($script:busyJob -and $script:busyJob.State -in @('Running', 'NotStarted')) { return $false }
    try {
        $script:busyJob = Start-Job -ScriptBlock $work -ArgumentList $argsList
        $script:busyDone = $done
        $script:notify.BalloonTipTitle = $title
        $script:notify.BalloonTipText = '正在执行，完成后提示...'
        $script:notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
        $script:notify.ShowBalloonTip(1800)
        return $true
    } catch {
        Write-TrayLog "Start-BusyJob($title) 失败: $($_.Exception.Message)"
        return $false
    }
}

function Check-BusyJob {
    if (-not $script:busyJob) { return }
    try {
        $st = $script:busyJob.State
        if ($st -eq 'Completed') {
            $out = Receive-Job $script:busyJob -Keep 2>$null | Out-String
            Remove-Job $script:busyJob -Force 2>$null
            $cb = $script:busyDone
            $script:busyJob = $null; $script:busyDone = $null
            if ($cb) { & $cb $out }
        } elseif ($st -in @('Failed', 'Stopped')) {
            $err = Receive-Job $script:busyJob 2>$null | Out-String
            Remove-Job $script:busyJob -Force 2>$null
            $script:busyJob = $null; $script:busyDone = $null
            [System.Windows.Forms.MessageBox]::Show("执行失败: $err", '错误', 'OK', 'Error') | Out-Null
        }
    } catch {
        Write-TrayLog "Check-BusyJob 异常: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 菜单
function Add-StatusItem {
    param($menu, [string]$text, [string]$value, [string]$badge)
    $item = New-Object System.Windows.Forms.ToolStripMenuItem
    $item.Text = "$text  $value"
    $item.Enabled = $false
    if ($badge) {
        $item.BackColor = switch ($badge) {
            'ok'   { [System.Drawing.Color]::FromArgb(235, 250, 240) }
            'bad'  { [System.Drawing.Color]::FromArgb(253, 235, 235) }
            default { [System.Drawing.Color]::FromArgb(255, 250, 230) }
        }
    }
    $menu.DropDownItems.Add($item) | Out-Null
}

# LEGACY: 旧版全量重建菜单(保留参考, 不再调用)
function Build-Menu-Legacy {
    param([string]$state, [string]$label)
    $queue = Get-QueueInfo
    $pending = $queue[0]
    $events = $queue[1]
    $confirm = $queue[2]
    $total = $queue[3]
    $bridge = Get-BridgeInfo
    $bridgeProc = $bridge[0]; $hbMin = $bridge[1]; $bridgeOnline = $bridge[2]; $lastCall = $bridge[3]
    $lastPush = Get-CloudRobot

    $script:menu.Items.Clear()

    # 总状态头
    $head = New-Object System.Windows.Forms.ToolStripMenuItem
    $head.Text = "StackChan Fusion  [$label]   $(Get-Date -Format 'HH:mm:ss')"
    $head.Enabled = $false
    $head.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9, [System.Drawing.FontStyle]::Bold)
    $script:menu.Items.Add($head) | Out-Null
    $script:menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    # Gateway 子菜单
    $gwMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $gwMenu.Text = "Gateway  $(if($script:gwOk){'● 在线'}else{'○ 离线'})"
    Add-StatusItem $gwMenu "PID:" "$($script:gwPid)" $(if($script:gwOk){'ok'}else{'bad'})
    Add-StatusItem $gwMenu "工具:" "$($script:tools.Count) 个" ''
    Add-StatusItem $gwMenu "播报队列:" "$pending 条" $(if($pending -gt 0){'warn'}else{'ok'})
    $script:menu.Items.Add($gwMenu) | Out-Null

    # MCP 子菜单
    $mcpMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $mcpMenu.Text = "MCP Toolkit  $(if($script:mcpOk){'● 正常'}else{'○ 异常'})"
    Add-StatusItem $mcpMenu "Profile:" "stackchan" $(if($script:mcpOk){'ok'}else{'bad'})
    Add-StatusItem $mcpMenu "客户端:" "codex / claude-code / vscode" ''
    $script:menu.Items.Add($mcpMenu) | Out-Null

    # 机器人链路子菜单 (云链路为主: xiaozhi.me 云端 + EMQX 主动推送; 自建服务器已停用)
    $robotMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $robotMenu.Text = "机器人链路  $(if($lastPush){'● 播报正常'}else{'○ 暂无推送'})"
    Add-StatusItem $robotMenu "播报链路:" $(if($lastPush){"最近推送 $lastPush"}else{'暂无推送记录'}) $(if($lastPush){'ok'}else{'warn'})
    Add-StatusItem $robotMenu "自建服务器:" '已停用(容器 Exited)' 'warn'
    Add-StatusItem $robotMenu "云桥接(备用):" "$bridgeProc 个进程" $(if($bridgeProc -ge 1){'ok'}else{'warn'})
    $hbBadge = if ($hbMin -ge 0 -and $hbMin -le 3) { 'ok' } elseif ($hbMin -ge 0) { 'warn' } else { 'bad' }
    Add-StatusItem $robotMenu "云心跳:" $(if($hbMin -ge 0){"$hbMin 分钟前"}else{'无'}) $hbBadge
    Add-StatusItem $robotMenu "接收消息:" $(if($lastCall){$lastCall}else{'暂无'}) ''
    $script:menu.Items.Add($robotMenu) | Out-Null

    # 消息/待处理子菜单
    $msgMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $msgMenu.Text = "播报队列"
    Add-StatusItem $msgMenu "待推送:" "$pending 条" $(if($pending -gt 0){'warn'}else{'ok'})
    Add-StatusItem $msgMenu "待播报事件:" "$events 条" $(if($events -gt 0){'warn'}else{'ok'})
    Add-StatusItem $msgMenu "待确认问题:" "$confirm 个" $(if($confirm -gt 0){'bad'}else{'ok'})
    if ($queue[4]) { Add-StatusItem $msgMenu "最近事件:" "$($queue[4])" '' }
    $script:menu.Items.Add($msgMenu) | Out-Null

    # 队列操作子菜单
    $queueMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $queueMenu.Text = "队列操作  ($total 条)"
    $itemShowQueue = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemShowQueue.Text = '显示队列消息内容...'
    $itemShowQueue.Add_Click({ Show-QueueMessages }) | Out-Null
    $queueMenu.DropDownItems.Add($itemShowQueue) | Out-Null
    $itemClearQueue = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemClearQueue.Text = '清空队列'
    $itemClearQueue.Add_Click({
        $r = [System.Windows.Forms.MessageBox]::Show(
            '确定清空待播报队列(事件 + 推送队列)？待确认问题保留。', '清空队列', 'YesNo', 'Warning')
        if ($r -eq 'Yes') { Clear-QueueData }
    }) | Out-Null
    $queueMenu.DropDownItems.Add($itemClearQueue) | Out-Null
    $itemClearConfirm = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemClearConfirm.Text = '清空待确认问题'
    $itemClearConfirm.Add_Click({
        $r = [System.Windows.Forms.MessageBox]::Show(
            '确定把所有未回答的 agent 待确认问题标记为已清理？', '清空待确认', 'YesNo', 'Warning')
        if ($r -eq 'Yes') {
            if (Test-Path -LiteralPath $confirmFile) {
                $items = Get-Content -Raw -LiteralPath $confirmFile -Encoding UTF8 | ConvertFrom-Json
                foreach ($c in $items) { if (-not $c.answered) { $c.answered = $true; $c.answer = 'cleared-from-tray' } }
                $items | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $confirmFile -Encoding UTF8
            }
            [System.Windows.Forms.MessageBox]::Show('待确认问题已清空。', '清空待确认', 'OK', 'Information') | Out-Null
        }
    }) | Out-Null
    $queueMenu.DropDownItems.Add($itemClearConfirm) | Out-Null
    $script:menu.Items.Add($queueMenu) | Out-Null

    $script:menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    $itemDetail = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemDetail.Text = '查看状态详情'
    $itemDetail.Add_Click({
        [System.Windows.Forms.MessageBox]::Show($script:detail, 'StackChan Fusion 状态', 'OK', 'Information') | Out-Null
    })
    $script:menu.Items.Add($itemDetail) | Out-Null

    # Claude hooks 自愈: 一键重跑 install_claude_hooks.ps1(hooks 主存 settings.json; ccswitch 切模型覆盖后需重装)
    $itemInstallHooks = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemInstallHooks.Text = '安装/修复 Claude Hooks'
    $itemInstallHooks.Add_Click({
        $installScript = Join-Path (Split-Path -Parent $root) 'agents\install_claude_hooks.ps1'
        if (-not (Test-Path -LiteralPath $installScript)) {
            [System.Windows.Forms.MessageBox]::Show("未找到安装脚本:`n$installScript", 'Claude Hooks', 'OK', 'Error') | Out-Null
            return
        }
        try {
            $hookOut = & $installScript 2>&1 | Out-String
            [System.Windows.Forms.MessageBox]::Show(
                "Claude hooks 已写入 settings.json。`n`n$($hookOut.Trim())`n`n提示: 若当前有正在运行的 Claude 会话, 请重启该会话以加载 hooks。",
                'Claude Hooks', 'OK', 'Information') | Out-Null
        } catch {
            [System.Windows.Forms.MessageBox]::Show("执行失败: $($_.Exception.Message)", 'Claude Hooks', 'OK', 'Error') | Out-Null
        }
    })
    $script:menu.Items.Add($itemInstallHooks) | Out-Null

    # Hook 自检与修复: 校验并自动修复 Antigravity/Claude/Codex hooks 配置 + 链路自检
    $itemHookHealth = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemHookHealth.Text = 'Hook 自检与修复'
    $itemHookHealth.Add_Click({
        $hh = Join-Path (Split-Path -Parent $root) 'scripts\hook_health.py'
        if (-not (Test-Path -LiteralPath $hh)) {
            [System.Windows.Forms.MessageBox]::Show("未找到自检脚本:`n$hh", 'Hook 自检', 'OK', 'Error') | Out-Null
            return
        }
        try {
            $env:PYTHONIOENCODING = 'utf-8'
            & 'C:\WINDOWS\py.exe' -3 $hh *> $null
            $result = Get-Content -LiteralPath (Join-Path $root 'state\hook_health.last.txt') `
                -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
            if (-not $result) { $result = '自检完成, 但未生成结果文件。' }
            [System.Windows.Forms.MessageBox]::Show($result.Trim(), 'Hook 自检与修复', 'OK', 'Information') | Out-Null
        } catch {
            [System.Windows.Forms.MessageBox]::Show("执行失败: $($_.Exception.Message)", 'Hook 自检', 'OK', 'Error') | Out-Null
        }
    })
    $script:menu.Items.Add($itemHookHealth) | Out-Null

    $itemRestart = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemRestart.Text = '重启网关'
    $itemRestart.Add_Click({
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'restart_gateway.ps1')`"") `
            -WindowStyle Hidden | Out-Null
        Start-Sleep -Seconds 1
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'run_gateway.ps1')`"") `
            -WindowStyle Hidden | Out-Null
    })
    $script:menu.Items.Add($itemRestart) | Out-Null

    $itemExit = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemExit.Text = '退出托盘'
    $itemExit.Add_Click({
        $script:notify.Visible = $false
        $script:timer.Stop()
        [System.Windows.Forms.Application]::Exit()
    })
    $script:menu.Items.Add($itemExit) | Out-Null
}

# ---------------------------------------------------------------- 轮询
# LEGACY: 旧版 UI 线程轮询(保留参考, 不再调用)
function Update-Status-Legacy {
    # 保险: Explorer 重启/通知区异常时, 图标可能消失, 轮询时重新置可见
    if ($script:notify -and -not $script:notify.Visible) { $script:notify.Visible = $true }
    try {
        $gwDetail = ''; $mcpDetail = ''; $robotDetail = ''
        $g = Test-Gateway -detailRef ([ref]$gwDetail)
        Restore-GatewayIfDown $g
        $m = Test-McpToolkit -detailRef ([ref]$mcpDetail)
        $bridge = Get-BridgeInfo
        $r = $bridge[2]
        Restore-BridgeIfDown $r

        if ($g -and $m -and $r) {
            $state = 'ok';   $label = '全部正常'
        } elseif ($g) {
            $state = 'warn'; $label = '部分异常'
        } else {
            $state = 'bad';  $label = '网关离线'
        }

        $queue = Get-QueueInfo
        $pending = $queue[0]
        $confirm = $queue[2]
        $total = $queue[3]
        $script:detail = "【Gateway】$gwDetail`n【MCP】$mcpDetail`n【Robot】bridge=$($bridge[0]) 进程, 心跳=$($bridge[1]) 分钟, 最近推送: $(Get-CloudRobot)`n【播报队列】待推送 $pending 条, 待播报事件 $($queue[1]) 条, 待确认 $confirm 个, 合计 $total 条"

        $script:notify.Icon = Get-StatusIcon $state $g $m $r
        $tooltip = "StackChan Fusion [$label]`n网关:$(if($g){'在线'}else{'离线'}) MCP:$(if($m){'正常'}else{'异常'}) 机器人:$(if($r){'在线'}else{'离线'})`n队列:$pending 待确认:$confirm"
        if ($script:notify.Text -ne $tooltip) { $script:notify.Text = $tooltip }

        Build-Menu $state $label

        if ($script:lastState -ne '' -and $script:lastState -ne $state) {
            $title = if ($state -eq 'ok') { 'StackChan 恢复' } elseif ($state -eq 'bad') { 'StackChan 网关离线' } else { 'StackChan 部分组件异常' }
            $script:notify.BalloonTipTitle = $title
            $script:notify.BalloonTipText = $script:detail
            $script:notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
            $script:notify.ShowBalloonTip(3000)
        }
        $script:lastState = $state
    } catch {
        Write-TrayLog "Update-Status 异常: $($_.Exception.Message)`n$($_.ScriptStackTrace)"
    }
}

# ---------------------------------------------------------------- 新菜单(缓存驱动, 状态变化才重建)
function Build-Menu {
    $s = $script:status
    $attached = if ($null -ne $script:attachOverride) { $script:attachOverride } else { $s.attached }
    $script:menu.Items.Clear()

    # 状态头
    $head = New-Object System.Windows.Forms.ToolStripMenuItem
    $head.Text = "StackChan Fusion  $(if($attached){'● 已连接'}else{'○ 已断开'})  [$($s.label)]  $(Get-Date -Format 'HH:mm:ss')"
    $head.Enabled = $false
    $head.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9, [System.Drawing.FontStyle]::Bold)
    $script:menu.Items.Add($head) | Out-Null
    $script:menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    # 连接开关(多机共用配置: 只保留一台连接机器人)
    $itemAttach = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemAttach.Text = if ($attached) { '连接机器人  ☑ 已连接' } else { '连接机器人  ☐ 已断开' }
    $itemAttach.Checked = $attached
    $itemAttach.ToolTipText = '多机共用同一配置时只保留一台连接; 断开期间消息入队不推流, 连接后自动补推。'
    $itemAttach.Add_Click({
        $cur = Get-CurrentStatus
        $target = -not ($cur -and $cur.attached)
        $work = {
            param($u, $t, $v)
            try {
                $b = @{ attached = $v } | ConvertTo-Json -Compress
                $r = Invoke-RestMethod -Uri "$u/api/robot_attach" -Method Post -Body $b -ContentType 'application/json' -Headers @{ Authorization = "Bearer $t" } -TimeoutSec 5
                return "attached=$($r.attached) pending=$($r.pending)"
            } catch { return "ERROR: $($_.Exception.Message)" }
        }
        $done = {
            param($out)
            if ($out -match '^attached=') {
                $script:attachOverride = ($out -match 'attached=True')
                $script:lastSig = ''  # 强制下个 UI 周期立即重建菜单
                [System.Windows.Forms.MessageBox]::Show("已切换: $($out.Trim())`n断开期间消息保留, 连接后自动补推。", '连接机器人', 'OK', 'Information') | Out-Null
            } else {
                [System.Windows.Forms.MessageBox]::Show($out.Trim(), '连接机器人', 'OK', 'Error') | Out-Null
            }
        }
        Start-BusyJob '连接机器人' $work $done -argsList @($gatewayUrl, $authToken, $target)
    })
    $script:menu.Items.Add($itemAttach) | Out-Null
    $script:menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    # Gateway
    $gwMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $gwMenu.Text = "Gateway  $(if($s.gwOk){'● 在线'}else{'○ 离线'})"
    Add-StatusItem $gwMenu "PID:" "$($s.gwPid)" $(if($s.gwOk){'ok'}else{'bad'})
    Add-StatusItem $gwMenu "工具:" "$($s.tools) 个" ''
    Add-StatusItem $gwMenu "播报队列:" "$($s.pending) 条" $(if($s.pending -gt 0){'warn'}else{'ok'})
    $script:menu.Items.Add($gwMenu) | Out-Null

    # MCP
    $mcpMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $mcpMenu.Text = "MCP Toolkit  $(if($s.mcpOk){'● 正常'}else{'○ 异常'})"
    Add-StatusItem $mcpMenu "Profile:" "stackchan" $(if($s.mcpOk){'ok'}else{'bad'})
    Add-StatusItem $mcpMenu "客户端:" "codex / claude-code / deepseek / agy / vscode" ''
    $script:menu.Items.Add($mcpMenu) | Out-Null

    # 机器人链路
    $robotMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $robotMenu.Text = "机器人链路  $(if($s.robotOnline){'● 在线'}else{'○ 离线'})"
    Add-StatusItem $robotMenu "机器人本体:" $(if($s.robotOnline){'在线'}else{'离线(最近无 ACK)'}) $(if($s.robotOnline){'ok'}else{'bad'})
    Add-StatusItem $robotMenu "本机连接:" $(if($attached){'已连接'}else{'已断开'}) $(if($attached){'ok'}else{'warn'})
    Add-StatusItem $robotMenu "最近推送:" $(if($s.lastPush){$s.lastPush}else{'暂无'}) $(if($s.lastPush){'ok'}else{'warn'})
    Add-StatusItem $robotMenu "最近播报:" $(if($s.lastBroadcast){$s.lastBroadcast}else{'暂无'}) $(if($s.lastBroadcast){'ok'}else{'warn'})
    Add-StatusItem $robotMenu "Hook 自检:" $(if($s.hookFault){'存在异常'}else{'正常'}) $(if($s.hookFault){'bad'}else{'ok'})
    Add-StatusItem $robotMenu "云桥接(备用):" "$($s.bridgeProc) 个进程" $(if($s.bridgeProc -ge 1){'ok'}else{'warn'})
    $hbBadge = if ($s.hbMin -ge 0 -and $s.hbMin -le 3) { 'ok' } elseif ($s.hbMin -ge 0) { 'warn' } else { 'bad' }
    Add-StatusItem $robotMenu "云心跳:" $(if($s.hbMin -ge 0){"$($s.hbMin) 分钟前"}else{'无'}) $hbBadge
    Add-StatusItem $robotMenu "接收消息:" $(if($s.lastCall){$s.lastCall}else{'暂无'}) ''
    $script:menu.Items.Add($robotMenu) | Out-Null

    # 播报队列
    $msgMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $msgMenu.Text = "播报队列  ($($s.total) 条)"
    Add-StatusItem $msgMenu "待推送:" "$($s.pending) 条" $(if($s.pending -gt 0){'warn'}else{'ok'})
    Add-StatusItem $msgMenu "待播报事件:" "$($s.events) 条" $(if($s.events -gt 0){'warn'}else{'ok'})
    Add-StatusItem $msgMenu "待确认问题:" "$($s.confirm) 个" $(if($s.confirm -gt 0){'bad'}else{'ok'})
    Add-StatusItem $msgMenu "播报历史:" "$($s.histCount) 条" ''
    $script:menu.Items.Add($msgMenu) | Out-Null

    $script:menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    # 队列操作
    $queueMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $queueMenu.Text = "队列操作"
    $itemShowQueue = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemShowQueue.Text = '显示队列消息内容...'
    $itemShowQueue.Add_Click({ Show-QueueMessages }) | Out-Null
    $queueMenu.DropDownItems.Add($itemShowQueue) | Out-Null
    $itemShowHistory = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemShowHistory.Text = '显示播报历史...'
    $itemShowHistory.Add_Click({ Show-BroadcastHistory }) | Out-Null
    $queueMenu.DropDownItems.Add($itemShowHistory) | Out-Null
    $itemClearQueue = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemClearQueue.Text = '清空队列'
    $itemClearQueue.Add_Click({
        $r = [System.Windows.Forms.MessageBox]::Show('确定清空待播报队列(事件 + 推送队列)？待确认问题保留。', '清空队列', 'YesNo', 'Warning')
        if ($r -eq 'Yes') { Clear-QueueData }
    }) | Out-Null
    $queueMenu.DropDownItems.Add($itemClearQueue) | Out-Null
    $itemClearConfirm = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemClearConfirm.Text = '清空待确认问题'
    $itemClearConfirm.Add_Click({
        $r = [System.Windows.Forms.MessageBox]::Show('确定把所有未回答的 agent 待确认问题标记为已清理？', '清空待确认', 'YesNo', 'Warning')
        if ($r -eq 'Yes') {
            if (Test-Path -LiteralPath $confirmFile) {
                $items = Get-Content -Raw -LiteralPath $confirmFile -Encoding UTF8 | ConvertFrom-Json
                foreach ($c in $items) { if (-not $c.answered) { $c.answered = $true; $c.answer = 'cleared-from-tray' } }
                $items | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $confirmFile -Encoding UTF8
            }
            [System.Windows.Forms.MessageBox]::Show('待确认问题已清空。', '清空待确认', 'OK', 'Information') | Out-Null
        }
    }) | Out-Null
    $queueMenu.DropDownItems.Add($itemClearConfirm) | Out-Null
    $script:menu.Items.Add($queueMenu) | Out-Null

    # 状态详情
    $itemDetail = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemDetail.Text = '查看状态详情'
    $itemDetail.Add_Click({
        $cur = Get-CurrentStatus
        [System.Windows.Forms.MessageBox]::Show($(if($cur){$cur.detail}else{'状态采集器未运行。'}), 'StackChan Fusion 状态', 'OK', 'Information') | Out-Null
    })
    $script:menu.Items.Add($itemDetail) | Out-Null

    # Hook 自检与修复(后台执行, UI 不冻结)
    $itemHookHealth = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemHookHealth.Text = 'Hook 自检与修复'
    $itemHookHealth.Add_Click({
        $hh = Join-Path (Split-Path -Parent $root) 'scripts\hook_health.py'
        $resFile = Join-Path $root 'state\hook_health.last.txt'
        if (-not (Test-Path -LiteralPath $hh)) {
            [System.Windows.Forms.MessageBox]::Show("未找到自检脚本:`n$hh", 'Hook 自检', 'OK', 'Error') | Out-Null
            return
        }
        $work = {
            param($scriptPath, $res)
            $env:PYTHONIOENCODING = 'utf-8'
            & 'C:\WINDOWS\py.exe' -3 $scriptPath *> $null
            $out = Get-Content -Raw -LiteralPath $res -Encoding UTF8 -ErrorAction SilentlyContinue
            if (-not $out) { $out = '自检完成, 但未生成结果文件。' }
            return $out.Trim()
        }
        $done = { param($out) [System.Windows.Forms.MessageBox]::Show($out.Trim(), 'Hook 自检与修复', 'OK', 'Information') | Out-Null }
        Start-BusyJob 'Hook 自检与修复' $work $done -argsList @($hh, $resFile)
    })
    $script:menu.Items.Add($itemHookHealth) | Out-Null

    # 安装/修复 Claude Hooks(后台执行)
    $itemInstallHooks = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemInstallHooks.Text = '安装/修复 Claude Hooks'
    $itemInstallHooks.Add_Click({
        $installScript = Join-Path (Split-Path -Parent $root) 'agents\install_claude_hooks.ps1'
        if (-not (Test-Path -LiteralPath $installScript)) {
            [System.Windows.Forms.MessageBox]::Show("未找到安装脚本:`n$installScript", 'Claude Hooks', 'OK', 'Error') | Out-Null
            return
        }
        $work = {
            param($s)
            return (& $s 2>&1 | Out-String).Trim()
        }
        $done = {
            param($out)
            [System.Windows.Forms.MessageBox]::Show(
                "Claude hooks 已写入 settings.json。`n`n$out`n`n提示: 若当前有正在运行的 Claude 会话, 请重启该会话以加载 hooks。",
                'Claude Hooks', 'OK', 'Information') | Out-Null
        }
        Start-BusyJob '安装/修复 Claude Hooks' $work $done -argsList @($installScript)
    })
    $script:menu.Items.Add($itemInstallHooks) | Out-Null

    # 重启网关
    $itemRestart = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemRestart.Text = '重启网关'
    $itemRestart.Add_Click({
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'restart_gateway.ps1')`"") `
            -WindowStyle Hidden | Out-Null
        # restart_gateway.ps1 handles stop-wait-start atomically.
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'run_gateway.ps1')`"") `
            -WindowStyle Hidden | Out-Null
    })
    $script:menu.Items.Add($itemRestart) | Out-Null

    # 一键重启激活所有服务(网关 + 云桥 MCP + Funnel 代理; 后台执行)
    $itemRestartAll = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemRestartAll.Text = '重启激活所有服务'
    $itemRestartAll.ToolTipText = '依次重启网关、云桥 MCP、Funnel 代理, 并等待网关恢复健康。'
    $itemRestartAll.Add_Click({
        $mcpDir = Join-Path (Split-Path -Parent $root) 'xiaozhi-mcp'
        $srvDir = Join-Path (Split-Path -Parent $root) 'server'
        $work = {
            param($gwDir, $mcpDir, $srvDir)
            function Run-Hidden([string]$file) {
                try {
                    $p = Start-Process -FilePath 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
                        -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$file`"") `
                        -WindowStyle Hidden -Wait -PassThru
                    return $p.ExitCode
                } catch { return -1 }
            }
            $lines = @()
            $rc = Run-Hidden (Join-Path $gwDir 'restart_gateway.ps1'); Start-Sleep -Seconds 2
            $rc2 = Run-Hidden (Join-Path $gwDir 'run_gateway.ps1')
            $lines += "网关: stop=$rc start=$rc2"
            $rc3 = Run-Hidden (Join-Path $mcpDir 'stop_bridge.ps1'); Start-Sleep -Seconds 2
            $rc4 = Run-Hidden (Join-Path $mcpDir 'run_bridge.ps1')
            $lines += "云桥 MCP: stop=$rc3 start=$rc4"
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like '*funnel_proxy.py*' } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 2
            $rc5 = Run-Hidden (Join-Path $srvDir 'start_funnel_proxy.ps1')
            $lines += "Funnel 代理: start=$rc5"
            $gwOk = $false
            for ($i = 0; $i -lt 30; $i++) {
                try {
                    $r = Invoke-RestMethod -Uri 'http://127.0.0.1:8010/healthz' -UseBasicParsing -TimeoutSec 3
                    if ($r.status -eq 'ok') { $gwOk = $true; break }
                } catch { }
                Start-Sleep -Seconds 1
            }
            $lines += "网关健康: $(if($gwOk){'OK'}else{'超时未就绪'})"
            return ($lines -join "`n")
        }
        $done = { param($out) [System.Windows.Forms.MessageBox]::Show($out.Trim(), '重启激活所有服务', 'OK', 'Information') | Out-Null }
        Start-BusyJob '重启激活所有服务' $work $done -argsList @($root, $mcpDir, $srvDir)
    })
    $script:menu.Items.Add($itemRestartAll) | Out-Null

    $script:menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    $itemExit = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemExit.Text = '退出托盘'
    $itemExit.Add_Click({
        $script:notify.Visible = $false
        $script:timer.Stop()
        if ($script:busyTimer) { $script:busyTimer.Stop() }
        Stop-Collector
        [System.Windows.Forms.Application]::Exit()
    })
    $script:menu.Items.Add($itemExit) | Out-Null
}

# ---------------------------------------------------------------- 轮询(UI 线程: 只读缓存, 变化才重建菜单)
function Get-CurrentStatus {
    $statusPath = Join-Path $root 'state\tray_status.json'
    try {
        if (Test-Path -LiteralPath $statusPath) {
            $age = ((Get-Date) - (Get-Item -LiteralPath $statusPath).LastWriteTime).TotalSeconds
            if ($age -le 60) { return (Get-Content -Raw -LiteralPath $statusPath -Encoding UTF8 | ConvertFrom-Json) }
        }
    } catch { }
    return $null
}

function Update-Status {
    if ($script:notify -and -not $script:notify.Visible) { $script:notify.Visible = $true }
    $s = Get-CurrentStatus
    if (-not $s) {
        $s = @{ state = 'bad'; label = '采集器未运行'; gwOk = $false; mcpOk = $false; robotOk = $false
                gwPid = 0; tools = 0; pending = 0; events = 0; confirm = 0; total = 0
                lastPush = ''; lastCall = ''; bridgeProc = 0; hbMin = -1; attached = $true
                robotOnline = $true; hookFault = $false; detail = '状态采集器未运行, 请重启托盘。'
                histCount = 0; lastBroadcast = ''; lastBroadcastTs = ''; broadcastRecent = @() }
    }
    $attached = if ($null -ne $script:attachOverride) { $script:attachOverride } else { $s.attached }
    if ($null -ne $script:attachOverride -and $s.attached -eq $script:attachOverride) { $script:attachOverride = $null }
    $script:status = $s   # v08.27: Build-Menu 渲染真实快照(此前菜单永远读初始占位表, 显示全离线)
    $sig = "$($s.state)|$($s.gwPid)|$($s.pending)|$($s.events)|$($s.confirm)|$attached|$($s.lastPush)|$($s.hbMin)|$($s.robotOnline)|$($s.hookFault)|$($null -ne $script:busyJob)"
    try {
        $iconKey = "$($s.state)|$($s.gwOk)|$($s.mcpOk)|$($s.robotOk)"
        if ($script:lastIconKey -ne $iconKey) {
            $script:lastIconKey = $iconKey
            $script:notify.Icon = Get-StatusIcon $s.state $s.gwOk $s.mcpOk $s.robotOk
        }
        $tooltip = "StackChan Fusion [$($s.label)]`n网关:$(if($s.gwOk){'在线'}else{'离线'}) MCP:$(if($s.mcpOk){'正常'}else{'异常'}) 机器人:$(if($s.robotOnline){'在线'}else{'离线'})`n连接:$(if($attached){'已连接'}else{'已断开'}) 队列:$($s.pending)"
        if ($script:notify.Text -ne $tooltip) { $script:notify.Text = $tooltip }
        if ($sig -ne $script:lastSig) {
            $script:lastSig = $sig
            Build-Menu
        }
        if ($script:lastState -ne '' -and $script:lastState -ne $s.state) {
            $title = if ($s.state -eq 'ok') { 'StackChan 恢复' } elseif ($s.state -eq 'bad') { 'StackChan 网关离线' } else { 'StackChan 部分组件异常' }
            $script:notify.BalloonTipTitle = $title
            $script:notify.BalloonTipText = $s.detail
            $script:notify.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
            $script:notify.ShowBalloonTip(3000)
        }
        $script:lastState = $s.state
    } catch {
        Write-TrayLog "Update-Status 异常: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------- 界面
$script:notify = New-Object System.Windows.Forms.NotifyIcon
$script:notify.Icon = Get-StatusIcon 'warn' $false $false $false
$script:notify.Text = 'StackChan Fusion 启动中...'
$script:notify.Visible = $true
$script:notify.Add_MouseDoubleClick({
    $cur = Get-CurrentStatus
    [System.Windows.Forms.MessageBox]::Show($(if($cur){$cur.detail}else{'状态采集器未运行。'}), 'StackChan Fusion 状态', 'OK', 'Information') | Out-Null
})

$script:menu = New-Object System.Windows.Forms.ContextMenuStrip
$script:notify.ContextMenuStrip = $script:menu
Write-TrayLog "托盘初始化: menu=$($null -ne $script:menu) notify=$($null -ne $script:notify)"

# UI 线程: 状态刷新 20s(读缓存, 变化才重建菜单); 忙任务单独 1s 轮询(不冻结)
$script:timer = New-Object System.Windows.Forms.Timer
$script:timer.Interval = 20000
$script:timer.Add_Tick({
    Update-Status
})
$script:timer.Start()

$script:busyTimer = New-Object System.Windows.Forms.Timer
$script:busyTimer.Interval = 1000
$script:busyTimer.Add_Tick({
    Check-BusyJob
})
$script:busyTimer.Start()

# 后台采集器(独立进程): 每 5s 采集状态写入 state\tray_status.json, UI 只读该 JSON
$script:lastIconKey = ''
$script:lastSig = ''
$script:lastState = ''
$script:attachOverride = $null

function Start-Collector {
    $collector = Join-Path $root 'tray_collector.ps1'
    try {
        $p = Start-Process -FilePath 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$collector`"", "`"$root`"") `
            -WindowStyle Hidden -PassThru
        Write-TrayLog "tray_collector 已启动 PID=$($p.Id)"
    } catch {
        Write-TrayLog "tray_collector 启动失败: $($_.Exception.Message)"
    }
}

function Stop-Collector {
    $pidFile = Join-Path $root 'state\tray_collector.pid'
    if (Test-Path -LiteralPath $pidFile) {
        try {
            $cid = [int](Get-Content -LiteralPath $pidFile -ErrorAction Stop)
            Stop-Process -Id $cid -Force -ErrorAction SilentlyContinue
        } catch { }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

Write-TrayLog "进入消息循环"
Start-Collector
Update-Status
[System.Windows.Forms.Application]::Run()
Write-TrayLog "消息循环退出"
