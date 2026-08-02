$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Register-Task($name, $script, $trigger) {
    $run = Join-Path $root $script
    # -WindowStyle Hidden: 静默运行, 不弹 powershell 窗口
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$run`"" -WorkingDirectory $root
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host "已注册 $name"
}

# 1) 登录时启动网关(静默)
Register-Task 'StackChan-FusionGateway' 'run_gateway.ps1' (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)

# 2) 登录时启动系统托盘状态工具(托盘内置守护: 网关挂了自动拉起, 无需定时任务)
Register-Task 'StackChan-FusionTray' 'fusion_tray.ps1' (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)

Write-Host '全部注册完成。立即启动托盘...'
$tray = Join-Path $root 'fusion_tray.ps1'
$p = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"`"$tray`"") -WindowStyle Hidden -PassThru
Write-Host "托盘已启动 PID=$($p.Id)"
