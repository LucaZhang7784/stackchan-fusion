$ErrorActionPreference = "Continue"
$fwroot = "D:\ProcessCenter\StackChan\fusion.firmware.0731"
$project = "$fwroot\reference\stackchan-xiaozhi-firmware"
$tmp = "$fwroot\firmware\build-phase71"
$out = "$fwroot\firmware\post-fw-v1.1-phase7.1"
New-Item -ItemType Directory -Path $tmp,$out -Force | Out-Null

# 只读输入快照(排除 .git 减小体积)
$src = Join-Path $tmp ("src-" + (Get-Date -Format 'HHmmss'))
New-Item -ItemType Directory -Path $src -Force | Out-Null
robocopy $project $src /E /XD .git /NFL /NDL /NP /NJH | Out-Null

$log = Join-Path $tmp "build-v111.log"
Write-Host "Building (log: $log) ..."

# 构建在容器内进行: 只读源码 + 输出目录; idf.py 自动拉取 78__esp-wifi-connect 等 managed components
$cid = "stackchan_idf_build_v111"
docker rm -f $cid 2>$null | Out-Null
$runOut = docker run -d --name $cid `
    -v "${src}:/src:ro" `
    -v "${out}:/out" `
    -v "$fwroot\firmware\build_led_ci.sh:/build_ci.sh:ro" `
    -v "$fwroot\firmware\build-phase71\patches_ml307_web_socket.cc:/patches_ml307_web_socket.cc:ro" `
    espressif/idf:v5.5.2 bash /build_ci.sh 2>&1
Write-Host "docker run -> $runOut"
$rc = docker wait $cid
Write-Host "docker wait -> $rc"
docker logs $cid 2>&1 | Tee-Object -FilePath $log
docker rm -f $cid 2>&1 | Out-Null

if ($rc -ne "0") {
    Write-Host "BUILD FAILED (rc=$rc) - see $log"
    exit $rc
}

if (-not (Test-Path "$out\xiaozhi.bin")) {
    Write-Host "BUILD FAILED: xiaozhi.bin missing - see $log"
    exit 1
}

if (-not (Test-Path "$out\srmodels.bin") -or -not (Test-Path "$out\generated_assets.bin")) {
    Write-Host "WARNING: srmodels.bin or generated_assets.bin missing - falling back to v1.0.8-selfhost assets"
    if (-not (Test-Path "$out\srmodels.bin")) {
        Copy-Item "$fwroot\firmware\post-fw-v1.0.8-selfhost\srmodels.bin" $out -Force
    }
    if (-not (Test-Path "$out\generated_assets.bin")) {
        Copy-Item "$fwroot\firmware\post-fw-v1.0.8-selfhost\generated_assets.bin" $out -Force
    }
}

python -m esptool --chip esp32s3 merge_bin -o "$out\merged-binary.bin" `
    --flash_mode dio --flash_size 16MB --flash_freq 80m `
    0x0       "$out\bootloader.bin" `
    0x8000    "$out\partition-table.bin" `
    0xd000    "$out\ota_data_initial.bin" `
    0x10000   "$out\srmodels.bin" `
    0x410000  "$out\xiaozhi.bin" `
    0xa10000  "$out\generated_assets.bin"

Write-Host "BUILD DONE -> $out"
Get-ChildItem $out | Select-Object Name, Length
