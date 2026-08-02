$ErrorActionPreference = "Stop"
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.json"
$hookCmd = "python <PROJECT_DIR>\agents\claude_hook.py"

$settings = @{}
if (Test-Path $settingsPath) {
    $raw = Get-Content $settingsPath -Raw -Encoding UTF8
    if ($raw) {
        try { $settings = $raw | ConvertFrom-Json -AsHashtable } catch { $settings = @{} }
    }
}

if (-not $settings.ContainsKey("hooks")) {
    $settings["hooks"] = @{}
}

$hookDef = @(@{ type = "command"; command = $hookCmd; timeout = 30 })
foreach ($event in @("Stop", "SessionEnd", "Notification")) {
    if (-not $settings["hooks"].ContainsKey($event)) {
        $settings["hooks"][$event] = @()
    }
    $list = $settings["hooks"][$event]
    $exists = $false
    foreach ($item in $list) {
        if ($item.command -like "*claude_hook.py*") { $exists = $true }
    }
    if (-not $exists) {
        $list += $hookDef
    }
}

$backup = "$settingsPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
if (Test-Path $settingsPath) { Copy-Item $settingsPath $backup }
$settings | ConvertTo-Json -Depth 8 | Set-Content $settingsPath -Encoding UTF8
Write-Host "hooks installed -> $settingsPath (backup: $backup)"
