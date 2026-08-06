$ErrorActionPreference = "Stop"

# Claude Code 全局配置路径
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"

# 完整 Python 路径(与审计修正后的 ~/.claude/settings.json 一致, 不依赖 PATH 里的 python)
$python = "C:\Users\zhang.luca\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw "找不到 Python, 请先安装并配置 Python311" }
}
$hookScript = "D:\ProcessCenter\StackChan\fusion.firmware.0731\agents\claude_hook.py"
# 命令用正斜杠: Claude Code 在 Windows 上经 /usr/bin/bash 执行 hook,
# 反斜杠会被 bash 吃掉(实测报 command not found), 正斜杠在 cmd/bash 下都兼容。
$hookCmd = "`"" + ($python -replace '\\', '/') + "`" `"" + ($hookScript -replace '\\', '/') + "`""

# 读取现有配置(不存在则新建)
$settings = @{}
if (Test-Path $settingsPath) {
    $raw = Get-Content $settingsPath -Raw -Encoding UTF8
    if ($raw) {
        try { $settings = $raw | ConvertFrom-Json -AsHashtable } catch { $settings = @{} }
    }
}
if (-not $settings.ContainsKey("hooks")) { $settings["hooks"] = @{} }

# 标准 hooks 结构: 事件 -> [ { matcher: "", hooks: [ { timeout, type, command } ] } ]
# (旧版脚本写成单层 command[], 会被 Claude Code 静默忽略——这是回流 hook 从未生效的根因)
foreach ($event in @("Stop", "SessionEnd", "Notification", "PermissionRequest")) {
    $list = @()
    if ($settings["hooks"].ContainsKey($event)) { $list = @($settings["hooks"][$event]) }
    $exists = $false
    foreach ($entry in $list) {
        if ($entry.hooks -and @($entry.hooks | Where-Object { $_.command -like "*claude_hook.py*" }).Count -gt 0) {
            $exists = $true
            break
        }
    }
    if (-not $exists) {
        $list += @{
            matcher = ""
            hooks   = @(@{ timeout = 30; type = "command"; command = $hookCmd })
        }
    }
    $settings["hooks"][$event] = $list
}

$backup = "$settingsPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
if (Test-Path $settingsPath) { Copy-Item $settingsPath $backup }
$settings | ConvertTo-Json -Depth 8 | Set-Content $settingsPath -Encoding UTF8
Write-Host "hooks installed -> $settingsPath (backup: $backup)"
Write-Host "结构: Stop/SessionEnd/Notification/PermissionRequest 各含 matcher+hooks[] (claude_hook.py)"
