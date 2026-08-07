$ErrorActionPreference = "Stop"

# ── 目标文件: settings.json(用户级全局) ──────────────────────────────────────────
# 历史: 曾用 settings.local.json, 理由是避开 ccswitch 全量覆盖 settings.json。
# 但实测 Claude Code 2.1.x Windows 存在已知 BUG (anthropics/claude-code#64699):
#   编辑 settings.local.json 后所有 hooks 静默失效, 且重启/回滚都无法恢复
#   (本机 2.1.224 已复现: 17:35 升级后 hooks 彻底不触发)。
# 2026-08-07 实测: hooks 写入 settings.json 后 Stop/SessionEnd/Notification 正常触发。
# 因此 hooks 主存 settings.json; 若 ccswitch 切模型覆盖 settings.json 抹掉 hooks,
# 重跑本脚本即可自愈恢复(脚本为幂等合并写入, 保留 env/permissions 等既有字段)。
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"

# 完整 Python 路径(与审计修正后的一致, 不依赖 PATH 里的 python)
$python = "C:\Users\<USER>\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw "找不到 Python, 请先安装并配置 Python311" }
}
$hookScript = "<PROJECT_ROOT>\fusion.firmware.0731\agents\claude_hook.py"
# 命令用正斜杠: Claude Code 在 Windows 上经 bash 执行 hook 时反斜杠会被吃掉(实测 command not found)
$hookCmd = "`"" + ($python -replace '\\', '/') + "`" `"" + ($hookScript -replace '\\', '/') + "`""

# 读取现有配置(不存在则新建, 保留 env/permissions 等既有字段)
# 注意: 兼容 Windows PowerShell 5.1, 严禁 ConvertFrom-Json -AsHashtable(PS7 专属, 5.1 会抛错并被 catch 吞掉)
$settings = $null
if (Test-Path $settingsPath) {
    $raw = Get-Content $settingsPath -Raw -Encoding UTF8
    if ($raw) {
        try { $settings = $raw | ConvertFrom-Json } catch { $settings = $null }
    }
}
if ($null -eq $settings -or $settings -isnot [System.Management.Automation.PSCustomObject]) {
    $settings = [PSCustomObject]@{}
}
if (-not $settings.PSObject.Properties['hooks']) {
    $settings | Add-Member -NotePropertyName hooks -NotePropertyValue ([PSCustomObject]@{})
}
$hooks = $settings.hooks

# 标准 hooks 结构: 事件 -> [ { matcher: "", hooks: [ { timeout, type, command } ] } ]
foreach ($event in @("Stop", "SessionEnd", "Notification", "PermissionRequest")) {
    $list = @()
    if ($hooks.PSObject.Properties[$event]) { $list = @($hooks.$event) }
    $exists = $false
    foreach ($entry in $list) {
        if ($entry.hooks -and @($entry.hooks | Where-Object { $_.command -like "*claude_hook.py*" }).Count -gt 0) {
            $exists = $true
            break
        }
    }
    if (-not $exists) {
        $list += [PSCustomObject]@{
            matcher = ""
            hooks   = @([PSCustomObject]@{ timeout = 30; type = "command"; command = $hookCmd })
        }
    }
    $hooks | Add-Member -NotePropertyName $event -NotePropertyValue $list -Force
}

$backup = "$settingsPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
if (Test-Path $settingsPath) { Copy-Item $settingsPath $backup }
$settings | ConvertTo-Json -Depth 8 | Set-Content $settingsPath -Encoding UTF8
Write-Host "hooks installed -> $settingsPath (backup: $backup)"
Write-Host "结构: Stop/SessionEnd/Notification/PermissionRequest 各含 matcher+hooks[] (claude_hook.py)"
Write-Host ""
Write-Host "[自愈保障] hooks 主存 settings.json(2.1.x Windows 下 settings.local.json 有 #64699 静默失效 BUG)。"
Write-Host "ccswitch 切模型若全量覆盖 settings.json(仅 env), 会抹掉 hooks 段——此时重跑本脚本即可恢复。"
Write-Host "建议: 在 cc-switch 切模型后执行一次本脚本, 或添加计划任务每小时自愈(可选)。"