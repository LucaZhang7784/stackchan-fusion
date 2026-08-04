param(
    [string]$Port = "COM8",
    [ValidateSet("full", "app")]
    [string]$Mode = "app"
)
$ErrorActionPreference = "Stop"

$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin = Join-Path $dir "merged-binary.bin"
$app = Join-Path $dir "xiaozhi.bin"

if (-not (Test-Path $bin)) { Write-Host "merged-binary.bin not found in $dir"; exit 1 }

if ($Mode -eq "full") {
    Write-Host "[1/2] Erasing flash on $Port ..."
    python -m esptool --chip esp32s3 --port $Port erase_flash
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "[2/2] Writing merged-binary.bin @ 0x0 ..."
    python -m esptool --chip esp32s3 -b 460800 --port $Port --before default-reset --after hard-reset `
        write-flash --flash-mode dio --flash-size 16MB --flash-freq 80m 0x0 $bin
} else {
    Write-Host "[app-only] Writing xiaozhi.bin @ 0x410000 (only valid on post-fw layout) ..."
    python -m esptool --chip esp32s3 -b 460800 --port $Port --before default-reset --after hard-reset `
        write-flash 0x410000 $app
}

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Flash done. Unplug USB + battery for 30s, then power on."
