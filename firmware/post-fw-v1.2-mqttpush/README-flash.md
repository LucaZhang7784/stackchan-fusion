# 固件 v1.2-mqttpush 刷机说明（µ-law MQTT 主动播报 + Phase 8 动作/拍照）

当前云链路正在使用的固件。基于已跑通的 07.31 基座
（`reference/stackchan-xiaozhi-firmware-mqtt`，heavenchenggong 系 + 「阿松」+ LED 补丁）。

## 本版能力

1. **第二条 MQTT 推送链路**（`stackchan/{mac}/push`，EMQX 公共 broker）：
   - µ-law(G.711) 16KB/s 音频直出播放（绕开 Opus 解码器兼容问题）；
   - START 报头解析 msg_uid → 回发 `stackchan/{mac}/ack`（网关 ACK 点杀）；
   - MQTT buffer 8KB、poll 读超时 5s、keepalive 15s、lwIP 收窗口 16KB、
     播报期间关 WiFi 节能；SSID 智能路由（EMQX 首选 → LAN → Tailscale/Funnel）。
2. **Phase 8.1 动作联动**：`done/error` 广播 → 舵机点头 Nod；
   `question` → 头偏转 +15° TiltAsk；待机闲逛摆头 20s 一次。
3. **Phase 8.2 拍照**：收到拍照指令 → 板载摄像头拍 JPEG →
   `stackchan/{mac}/photo`（QoS1 分块）→ 网关重组校验。

## 刷机（app-only，保留配置）

```powershell
cd <PROJECT_DIR>\firmware\post-fw-v1.2-mqttpush
python -m esptool --chip esp32s3 -b 460800 --port COM8 `
  --before default-reset --after hard-reset write-flash 0x410000 xiaozhi.bin
```

全量（从其他固件切换时，会擦除、需重新配网）：

```powershell
python -m esptool --chip esp32s3 -b 460800 --port COM8 `
  --before default-reset --after hard-reset write-flash `
  0x0 bootloader.bin 0x8000 partition-table.bin 0xd000 ota_data_initial.bin `
  0x10000 srmodels.bin 0x410000 xiaozhi.bin 0xa10000 generated_assets.bin
```

## 重新构建

源码：`<PROJECT_DIR>\reference\stackchan-xiaozhi-firmware-mqtt`（或仓库
`firmware-src/`）；构建脚本 `<PROJECT_DIR>\firmware\build_fw_v112.ps1`
（espressif/idf:v5.5.2 容器；5.5.4 会黑屏）。

## 验证

1. 网关 push 一条消息，机器人应立即出声（µ-law 直出），STOP 后自然切回待机暖橙。
2. 网关日志出现 `push ack` 说明固件已回执 msg_uid（闭环成功）。
3. 机器人被推送 done 事件时点头、question 事件时歪头；待机摆头约 20s 一次。
4. `robot_snap` 拍照后网关校验 JPEG 魔数 + 总长度，返回图片。
