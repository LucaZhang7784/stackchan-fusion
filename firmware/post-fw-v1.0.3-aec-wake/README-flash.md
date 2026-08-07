# 固件 v1.0.3-aec-wake 刷机说明（AEC + 唤醒加速）

基于已跑通的 `reference/stackchan-xiaozhi-firmware`（07.31 基座 + micfix），
针对两个线上问题修复：

1. **唤醒后进入聆听慢（等几秒才出现「聆听中」）**
   - multinet 检测窗口 `duration_` 3000ms → **1500ms**
   - 唤醒阈值下限 0.35 → **0.30**（更灵敏、更快触发）
   - 新增**后台预热连接**：待机时维持一条 WebSocket 连接，
     唤醒时跳过重新握手（DNS/TLS/hello），直接进入聆听；
     断线后按 15s→30s→60s→120s 指数退避自动重连。
2. **识别准确率低 / 播报时听错**
   - 启用**设备端 AEC**（`CONFIG_USE_DEVICE_AEC=y`，Kconfig 已加入
     M5STACK_CORE_S3 板型支持）：
     - 播放 TTS 时用 ES7210 参考输入消除扬声器回声；
     - 聆听模式从 AutoStop 切换为 **Realtime**（可打断、不截尾音，减少丢字）。

## 改动清单

| 文件 | 改动 |
|---|---|
| `main/Kconfig.projbuild` | `USE_DEVICE_AEC` 依赖加入 `BOARD_TYPE_M5STACK_CORE_S3` |
| `sdkconfig.defaults` | `CONFIG_USE_DEVICE_AEC=y` |
| `main/audio/wake_words/custom_wake_word.h` | `duration_` 3000→1500；`threshold_` 0.35→0.30 |
| `main/audio/wake_words/custom_wake_word.cc` | 阈值下限 0.35→0.30 |
| `main/protocols/protocol.h` | `OpenAudioChannel(bool silent=false)`（后台连接不弹错误提示） |
| `main/protocols/websocket_protocol.{h,cc}` | 连接互斥锁 + silent 模式 + 失败清理 |
| `main/protocols/mqtt_protocol.{h,cc}` | 签名同步 + hello 超时 silent |
| `main/application.{h,cc}` | 后台预热连接任务（指数退避）；预热连接不强制高性能模式 |

## 刷机（app-only，保留配置）

```powershell
cd <PROJECT_DIR>\firmware\post-fw-v1.0.3-aec-wake
python -m esptool --chip esp32s3 -b 460800 --port COM8 `
  --before default-reset --after hard-reset write-flash 0x410000 xiaozhi.bin
```

或: `powershell -ExecutionPolicy Bypass -File .\flash_post_fw.ps1 -Port COM8 -Mode app`

全量（从其他固件切换时）: `-Mode full`（会擦除，需重新配网）。

## 验证

1. 开机后等约 15-30s（后台预热连接建立），说「阿松」应立即进入聆听（<1s）。
2. 唤醒后正常对话，机器人播报期间直接说话（打断），识别不应再把回声听成内容。
3. 若连续失败 3 次以上（唤醒词），可在串口日志看 `multinet detect threshold set to 0.30`。
4. 误唤醒变多时，把 `custom_wake_word.h` 的 `threshold_` 调回 0.33~0.35 再编译。

## 已知取舍

- 待机时常驻一条 WebSocket 连接（不深睡）。CoreS3 的 PowerSaveTimer 本机已禁用
  （`-1`），无影响；若未来需要电池深睡，需在进入深睡前主动 `CloseAudioChannel`。
- 识别准确率还受云端 ASR 影响；若本地环境噪音大，可再评估加入 NS 降噪模型
  （需重排 model 分区，当前 4MB 分区已装 3.8MB，装不下）。
