$ErrorActionPreference = "SilentlyContinue"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = Join-Path $root 'config.json'
$gatewayUrl = 'http://127.0.0.1:8010'
$authToken = ''
try {
    $cfg = Get-Content -Raw -LiteralPath $configPath | ConvertFrom-Json
    $authToken = $cfg.auth_token
} catch { }
if (-not $authToken) { $authToken = 'YOUR_GATEWAY_TOKEN' }

$bridgeErr = '<PROJECT_DIR>\xiaozhi-mcp\bridge.err'
$pendingFile = Join-Path $root 'state\pending.jsonl'
$eventsFile = Join-Path $root 'data\agent_events.jsonl'
$confirmFile = Join-Path $root 'data\agent_confirmations.json'

$script:lastState = ''              # 上一次总状态
$script:lastRestartAt = [DateTime]::MinValue
$script:gwOk = $false
$script:mcpOk = $false
$script:robotOk = $false
$script:tools = @()
$script:gwPid = 0
$script:detail = ''

# ---------------------------------------------------------------- 状态采集
function Test-Gateway {
    param([ref]$detailRef)
    try {
        $r = Invoke-RestMethod -Uri "$gatewayUrl/healthz" -Headers @{ Authorization = "Bearer $authToken" } -TimeoutSec 3
        $script:gwOk = ($r.status -eq 'ok')
        $script:tools = @($r.tools)
        $script:gwPid = $r.pid
        $detailRef.Value = "PID=$($r.pid) 启动=$($r.started_at) 工具=$($script:tools.Count) 个"
        return $script:gwOk
    } catch {
        $script:gwOk = $false
        $script:tools = @()
        $script:gwPid = 0
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
        $tail = Get-Content -LiteralPath $bridgeErr -Tail 200 -ErrorAction SilentlyContinue
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

function Get-PendingCount {
    if (-not (Test-Path -LiteralPath $pendingFile)) { return 0 }
    return @(Get-Content -LiteralPath $pendingFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() }).Count
}

function Get-EventsInfo {
    # 返回 (总数, 最近一条摘要)
    if (-not (Test-Path -LiteralPath $eventsFile)) { return @(0, '') }
    $lines = @(Get-Content -LiteralPath $eventsFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) { return @(0, '') }
    $last = $lines[-1]
    $summary = ''
    try {
        $o = $last | ConvertFrom-Json
        $summary = "[$($o.agent) $($o.type)] $($o.summary)"
        if ($summary.Length -gt 60) { $summary = $summary.Substring(0, 60) + '...' }
    } catch { }
    return @($lines.Count, $summary)
}

function Get-ConfirmCount {
    if (-not (Test-Path -LiteralPath $confirmFile)) { return 0 }
    try {
        $items = Get-Content -Raw -LiteralPath $confirmFile | ConvertFrom-Json
        return @($items | Where-Object { -not $_.answered }).Count
    } catch { return 0 }
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

# ---------------------------------------------------------------- 图标
function Get-StatusIcon {
    param([string]$state, [bool]$gw, [bool]$mcp, [bool]$robot)
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
    $bgRect = New-Object System.Drawing.Rectangle 1, 1, 30, 30
    $brush = New-Object System.Drawing.Drawing2D.LinearGradientBrush $bgRect, $c1, $c2, 45
    $path = New-Object System.Drawing.Drawing2D.GraphicsPath
    $d = 14
    $path.AddArc(1, 1, $d, $d, 180, 90)
    $path.AddArc(17, 1, $d, $d, 270, 90)
    $path.AddArc(17, 17, $d, $d, 0, 90)
    $path.AddArc(1, 17, $d, $d, 90, 90)
    $path.CloseFigure()
    $g.FillPath($brush, $path)

    # 三个状态点: 网关 / MCP / 机器人 (上 2 下 1)
    $on  = [System.Drawing.Color]::White
    $off = [System.Drawing.Color]::FromArgb(255, 255, 255, 160)
    $dotBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
    $dotOff   = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(120, 255, 255, 255))
    $positions = @(
        @{ X = 11; Y = 11; On = $gw },
        @{ X = 19; Y = 11; On = $mcp },
        @{ X = 15; Y = 19; On = $robot }
    )
    foreach ($p in $positions) {
        $b = if ($p.On) { $dotBrush } else { $dotOff }
        $g.FillEllipse($b, $p.X, $p.Y, 5, 5)
    }
    $g.Dispose()
    $hicon = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($hicon)
    $bmp.Dispose()
    return $icon
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

function Build-Menu {
    param([string]$state, [string]$label)
    $pending = Get-PendingCount
    $events = Get-EventsInfo
    $confirm = Get-ConfirmCount
    $bridge = Get-BridgeInfo
    $bridgeProc = $bridge[0]; $hbMin = $bridge[1]; $bridgeOnline = $bridge[2]; $lastCall = $bridge[3]

    $menu.DropDownItems.Clear()

    # 总状态头
    $head = New-Object System.Windows.Forms.ToolStripMenuItem
    $head.Text = "StackChan Fusion  [$label]   $(Get-Date -Format 'HH:mm:ss')"
    $head.Enabled = $false
    $head.Font = New-Object System.Drawing.Font('Microsoft YaHei', 9, [System.Drawing.FontStyle]::Bold)
    $menu.DropDownItems.Add($head) | Out-Null
    $menu.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    # Gateway 子菜单
    $gwMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $gwMenu.Text = "Gateway  $(if($script:gwOk){'● 在线'}else{'○ 离线'})"
    Add-StatusItem $gwMenu "PID:" "$($script:gwPid)" $(if($script:gwOk){'ok'}else{'bad'})
    Add-StatusItem $gwMenu "工具:" "$($script:tools.Count) 个" ''
    Add-StatusItem $gwMenu "队列消息:" "$pending 条" $(if($pending -gt 0){'warn'}else{'ok'})
    $menu.DropDownItems.Add($gwMenu) | Out-Null

    # MCP 子菜单
    $mcpMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $mcpMenu.Text = "MCP Toolkit  $(if($script:mcpOk){'● 正常'}else{'○ 异常'})"
    Add-StatusItem $mcpMenu "Profile:" "stackchan" $(if($script:mcpOk){'ok'}else{'bad'})
    Add-StatusItem $mcpMenu "客户端:" "codex / claude-code / vscode" ''
    $menu.DropDownItems.Add($mcpMenu) | Out-Null

    # 机器人链路子菜单
    $robotMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $robotMenu.Text = "机器人链路  $(if($bridgeOnline){'● 在线'}else{'○ 离线'})"
    Add-StatusItem $robotMenu "Bridge进程:" "$bridgeProc 个" $(if($bridgeProc -ge 1){'ok'}else{'bad'})
    $hbBadge = if ($hbMin -ge 0 -and $hbMin -le 3) { 'ok' } elseif ($hbMin -ge 0) { 'warn' } else { 'bad' }
    Add-StatusItem $robotMenu "心跳:" $(if($hbMin -ge 0){"$hbMin 分钟前"}else{'无'}) $hbBadge
    Add-StatusItem $robotMenu "接收消息:" $(if($lastCall){$lastCall}else{'暂无'}) ''
    $menu.DropDownItems.Add($robotMenu) | Out-Null

    # 消息/待处理子菜单
    $msgMenu = New-Object System.Windows.Forms.ToolStripMenuItem
    $msgMenu.Text = "消息与待办"
    Add-StatusItem $msgMenu "待推送队列:" "$pending 条" $(if($pending -gt 0){'warn'}else{'ok'})
    Add-StatusItem $msgMenu "agent 事件:" "$($events[0]) 条" ''
    Add-StatusItem $msgMenu "待确认问题:" "$confirm 个" $(if($confirm -gt 0){'bad'}else{'ok'})
    if ($events[1]) { Add-StatusItem $msgMenu "最近事件:" "$($events[1])" '' }
    $menu.DropDownItems.Add($msgMenu) | Out-Null

    $menu.DropDownItems.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

    $itemDetail = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemDetail.Text = '查看状态详情'
    $itemDetail.Add_Click({
        [System.Windows.Forms.MessageBox]::Show($script:detail, 'StackChan Fusion 状态', 'OK', 'Information') | Out-Null
    })
    $menu.DropDownItems.Add($itemDetail) | Out-Null

    $itemRestart = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemRestart.Text = '重启网关'
    $itemRestart.Add_Click({
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'stop_gateway.ps1')`"") `
            -WindowStyle Hidden | Out-Null
        Start-Sleep -Seconds 1
        Start-Process -FilePath 'powershell.exe' `
            -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$(Join-Path $root 'run_gateway.ps1')`"") `
            -WindowStyle Hidden | Out-Null
    })
    $menu.DropDownItems.Add($itemRestart) | Out-Null

    $itemExit = New-Object System.Windows.Forms.ToolStripMenuItem
    $itemExit.Text = '退出托盘'
    $itemExit.Add_Click({
        $notify.Visible = $false
        $timer.Stop()
        [System.Windows.Forms.Application]::Exit()
    })
    $menu.DropDownItems.Add($itemExit) | Out-Null
}

# ---------------------------------------------------------------- 轮询
function Update-Status {
    $gwDetail = ''; $mcpDetail = ''; $robotDetail = ''
    $g = Test-Gateway -detailRef ([ref]$gwDetail)
    Restore-GatewayIfDown $g
    $m = Test-McpToolkit -detailRef ([ref]$mcpDetail)
    $bridge = Get-BridgeInfo
    $r = $bridge[2]

    if ($g -and $m -and $r) {
        $state = 'ok';   $label = '全部正常'
    } elseif ($g) {
        $state = 'warn'; $label = '部分异常'
    } else {
        $state = 'bad';  $label = '网关离线'
    }

    $pending = Get-PendingCount
    $confirm = Get-ConfirmCount
    $script:detail = "【Gateway】$gwDetail`n【MCP】$mcpDetail`n【Robot】bridge=$($bridge[0]) 进程, 心跳=$($bridge[1]) 分钟`n【队列】待推送 $pending 条, 待确认 $confirm 个"

    $notify.Icon = Get-StatusIcon $state $g $m $r
    $tooltip = "StackChan Fusion [$label]`n网关:$(if($g){'在线'}else{'离线'}) MCP:$(if($m){'正常'}else{'异常'}) 机器人:$(if($r){'在线'}else{'离线'})`n队列:$pending 待确认:$confirm"
    if ($notify.Text -ne $tooltip) { $notify.Text = $tooltip }

    Build-Menu $state $label

    if ($script:lastState -ne '' -and $script:lastState -ne $state) {
        $title = if ($state -eq 'ok') { 'StackChan 恢复' } elseif ($state -eq 'bad') { 'StackChan 网关离线' } else { 'StackChan 部分组件异常' }
        $notify.ShowBalloonTip(3000, $title, $script:detail, [System.Windows.Forms.ToolTipIcon]::Warning)
    }
    $script:lastState = $state
}

# ---------------------------------------------------------------- 界面
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = Get-StatusIcon 'warn' $false $false $false
$notify.Text = 'StackChan Fusion 启动中...'
$notify.Visible = $true
$notify.Add_MouseDoubleClick({
    [System.Windows.Forms.MessageBox]::Show($script:detail, 'StackChan Fusion 状态', 'OK', 'Information') | Out-Null
})

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$notify.ContextMenuStrip = $menu

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.Add_Tick({ Update-Status })
$timer.Start()

Update-Status
[System.Windows.Forms.Application]::Run()
