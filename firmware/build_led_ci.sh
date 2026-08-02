#!/bin/bash
set -e
. $IDF_PATH/export.sh >/dev/null 2>&1
cp -a /src/. /work/
cd /work
rm -rf /work/build
idf.py set-target esp32s3
idf.py reconfigure || true
if [ -d /stash ] && [ -d managed_components/78__esp-wifi-connect/assets ]; then
  cp -r /stash/* managed_components/78__esp-wifi-connect/assets/ || true
fi
idf.py build
mkdir -p /out
cp build/bootloader/bootloader.bin /out/
cp build/partition_table/partition-table.bin /out/
cp build/ota_data_initial.bin /out/
cp build/StackChan-XiaoZhi.bin /out/xiaozhi.bin
cp build/srmodels/srmodels.bin /out/ 2>/dev/null || true
cp build/generated_assets.bin /out/ 2>/dev/null || true
echo "ARTIFACTS_COPIED"
