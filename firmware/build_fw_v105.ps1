$ErrorActionPreference = "Continue"
$fwroot = "<PROJECT_DIR>"
$project = "$fwroot\reference\stackchan-xiaozhi-firmware"
$tmp = "$fwroot\firmware\build-led-tmp"
$out = "$fwroot\firmware\post-fw-v1.0.5-gain36"
New-Item -ItemType Directory -Path $tmp,$out -Force | Out-Null

$mc = "$project\managed_components"
if (Test-Path $mc) {
    if (-not (Test-Path "$tmp\managed_components_backup")) {
        Copy-Item $mc "$tmp\managed_components_backup" -Recurse -Force
    }
    Remove-Item $mc -Recurse -Force
}

# 准备只读输入目录(排除 .git 减小体积)
$src = "$tmp\src-v105"
New-Item -ItemType Directory -Path $src -Force | Out-Null
robocopy $project $src /E /XD .git managed_components /NFL /NDL /NP /NJH | Out-Null

$log = "$tmp\build-v105.log"
Write-Host "Building (log: $log) ..."

# 构建在容器文件系统内进行(挂载仅用于只读输入 + 输出), 避开 Windows 挂载删目录问题
$cid = "stackchan_idf_build_v105"
docker rm -f $cid 2>$null | Out-Null
$runOut = docker run -d --name $cid -v "${src}:/src:ro" -v "$tmp\managed_components_backup:/stash:ro" -v "${out}:/out" -v "$fwroot\firmware\build_led_ci.sh:/build_ci.sh:ro" espressif/idf:v5.5.2 bash /build_ci.sh 2>&1
Write-Host "docker run -> $runOut"
$rc = docker wait $cid
Write-Host "docker wait -> $rc"
docker logs $cid 2>&1 | Tee-Object -FilePath $log
docker rm -f $cid 2>&1 | Out-Null

if ($rc -ne "0") {
    Write-Host "BUILD FAILED (rc=$rc) - see $log"
    exit $rc
}

if (-not (Test-Path "$out\srmodels.bin") -or -not (Test-Path "$out\generated_assets.bin")) {
    Write-Host "WARNING: srmodels.bin or generated_assets.bin missing - falling back to release assets"
    if (-not (Test-Path "$out\srmodels.bin")) {
        Copy-Item "$fwroot\firmware\post-fw-v1.0.0\srmodels.bin" $out -Force
    }
    if (-not (Test-Path "$out\generated_assets.bin")) {
        Copy-Item "$fwroot\firmware\post-fw-v1.0.0\generated_assets.bin" $out -Force
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
