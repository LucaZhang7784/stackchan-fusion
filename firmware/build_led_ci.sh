#!/bin/bash
set -e
. $IDF_PATH/export.sh >/dev/null 2>&1
cp -a /src/. /work/
cd /work
rm -rf /work/build
idf.py set-target esp32s3
idf.py reconfigure || true
# Phase 7.1: 覆盖 78__esp-ml307 WebSocket 握手超时(10s→5s), 与总连接看门狗配合
if [ -f /patches_ml307_web_socket.cc ] && [ -f /work/managed_components/78__esp-ml307/src/web_socket.cc ]; then
  cp /patches_ml307_web_socket.cc /work/managed_components/78__esp-ml307/src/web_socket.cc
fi
if [ -d /stash ] && [ -d managed_components/78__esp-wifi-connect/assets ]; then
  cp -r /stash/* managed_components/78__esp-wifi-connect/assets/ || true
fi
idf.py build
mkdir -p /out
cp build/bootloader/bootloader.bin /out/
cp build/partition_table/partition-table.bin /out/
cp build/ota_data_initial.bin /out/
cp build/StackChan-XiaoZhi.bin /out/xiaozhi.bin
cp build/*.elf /out/xiaozhi.elf 2>/dev/null || true
cp build/srmodels/srmodels.bin /out/ 2>/dev/null || true
cp build/generated_assets.bin /out/ 2>/dev/null || true
echo "ARTIFACTS_COPIED"
