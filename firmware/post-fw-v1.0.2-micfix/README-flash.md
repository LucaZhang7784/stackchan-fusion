# 固件 v1.0.2-micfix 刷机说明（基于已跑通基座, 仅改麦克风增益）

**重要: 本版不是从 HtSz 主分支构建的**, 而是以 2026-07-31 已跑通的
`reference/stackchan-xiaozhi-firmware`（heavenchenggong 系, 含本地「阿松」+ LED 补丁）
为基座, 在 `cores3_audio_codec.cc` 里只改了一处: 麦克风输入增益 30 → 42。

二进制尺寸与 v1.0.0-led 完全一致（xiaozhi.bin 2,961,376 / merged 15,556,642）,
唤醒词「阿松」、LED、阈值下限 0.35 防误触发等行为全部保留。

## 改动

| 项 | 旧值 | 新值 |
|---|---|---|
| 麦克风输入增益 `input_gain_` | 30 | **42** |

## 刷机（app-only, 保留配置）

```powershell
cd <PROJECT_DIR>\firmware\post-fw-v1.0.2-micfix
python -m esptool --chip esp32s3 -b 460800 --port COM8 `
  --before default-reset --after hard-reset write-flash 0x410000 xiaozhi.bin
```

或: `powershell -ExecutionPolicy Bypass -File .\flash_post_fw.ps1 -Port COM8 -Mode app`

全量（从其他固件切换时）: `-Mode full`（会擦除, 需重新配网）。

## 验证

1. 机器人正常启动（屏幕亮、连接服务器）。
2. 说「阿松」唤醒。
3. 距 0.5-1 米说话, 识别应比增益 30 时清晰。
