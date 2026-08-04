$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# 用 wscript.exe + VBS 隐藏启动器注册任务:
# 计划任务直接跑 powershell 时, 即使带 -WindowStyle Hidden 仍可能闪一下控制台窗口;
# wscript.exe 是 GUI 子系统, 本身没有控制台, 再用窗口样式 0 启动 powershell,
# 从物理上杜绝弹窗(2026-08-03 实测修复)。
function Register-VbsTask($name, $vbs, $trigger) {
    $action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`""
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "已注册 $name (wscript 隐藏启动)"
}

# 1) 登录时启动网关(静默)
Register-VbsTask 'StackChan-FusionGateway' (Join-Path $root 'run_gateway_hidden.vbs') `
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)

# 2) 登录时启动系统托盘状态工具(托盘内置守护: 网关挂了自动拉起, 无需定时任务)
Register-VbsTask 'StackChan-FusionTray' (Join-Path $root 'fusion_tray_hidden.vbs') `
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)

# 3) 本地备用路由: funnel 代理开机自启 + 每 5 分钟自愈
$funnelRoot = Join-Path (Split-Path -Parent $root) 'server'
$funnelAction = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument "`"$(Join-Path $funnelRoot 'watchdog_funnel_proxy_hidden.vbs')`""
$funnelTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))
)
$funnelSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'StackChan-FunnelProxyWatchdog' `
    -Action $funnelAction -Trigger $funnelTriggers -Settings $funnelSettings -Force | Out-Null
Write-Host '已注册 StackChan-FunnelProxyWatchdog(登录自启 + 5 分钟自愈, wscript 隐藏启动)'

# 4) 云桥接(机器人走 xiaozhi.me 时必需, 电脑重启后自动拉起)
$bridgeVbs = Join-Path (Split-Path -Parent $root) 'xiaozhi-mcp\run_bridge_hidden.vbs'
Register-VbsTask 'StackChan-CloudBridge' $bridgeVbs `
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)

Write-Host '全部注册完成。立即启动托盘...'
$tray = Join-Path $root 'fusion_tray.ps1'
$p = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$tray`"") -WindowStyle Hidden -PassThru
Write-Host "托盘已启动 PID=$($p.Id)"
