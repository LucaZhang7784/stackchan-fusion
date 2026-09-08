$ErrorActionPreference = "Stop"
Write-Host "=============================================="
Write-Host "Codex: uninstall Store version, keep CLI + data"
Write-Host "=============================================="

$backup = "D:\ProcessCenter\StackChan\fusion.firmware.0731\backup-codex-20260801"
if (-not (Test-Path (Join-Path $backup "auth.json"))) {
    Write-Host "ABORT: backup missing. Run the backup step first."
    exit 1
}
Write-Host "[1/4] Backup OK: $backup"

$pkg = Get-AppxPackage | Where-Object { $_.Name -eq "OpenAI.Codex" }
if (-not $pkg) {
    Write-Host "[2/4] Store package not found (already removed?)"
} else {
    Write-Host "[2/4] Removing Store package: $($pkg.PackageFullName)"
    Remove-AppxPackage -Package $pkg.PackageFullName
    Write-Host "      Removed."
}

Start-Sleep -Seconds 2
Write-Host "[3/4] Verifying CLI resolution..."
$cli = (Get-Command codex -ErrorAction SilentlyContinue).Source
Write-Host "      codex -> $cli"
$ver = codex --version 2>&1 | Select-Object -First 1
Write-Host "      version: $ver"

Write-Host "[4/4] Verifying data intact..."
$auth = Test-Path "$env:USERPROFILE\.codex\auth.json"
$cfg = Test-Path "$env:USERPROFILE\.codex\config.toml"
$sess = (Get-ChildItem "$env:USERPROFILE\.codex\sessions" -Recurse -File -ErrorAction SilentlyContinue).Count
Write-Host "      auth.json: $auth | config.toml: $cfg | session files: $sess"

if ($cli -like "*AppData\Roaming\npm*" -and $auth) {
    Write-Host "SUCCESS: Codex CLI active, data preserved."
} else {
    Write-Host "WARNING: check resolution or data path manually."
}
