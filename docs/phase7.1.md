# Phase 7.1 · 固件与底层性能攻坚（低延迟 / 硬件打断 / 首字不丢 / LED 同步）

> 2026-08-05 实施。依据 Antigravity 审计报告（`stackchan_audit_report.md`）第四、五部分，
> 暂停新功能，聚焦底层性能重构。共经历三轮审查，最终约束全部落实。

### 2026-08-05 补充修正（第四轮审查 4 点）
1. **TTS 首句切分补逗号**：`tts/base.py` 切分正则补全 `， , 、`，保证 "好的，" 这类首句 100ms 内触发首包 TTS。
2. **manual 模式 6s→800ms**：`aliyunbl_stream.py` 双击(手动)模式句末静默 `max_sentence_silence` 由 6000ms 降为 800ms，消除说完话干等 6 秒。
3. **ASR 缓存上限 800ms**：`asr/base.py` 的 `conn.asr_audio` 仅保留最近 13 帧(60ms/帧≈780ms)，防网络波动/手动模式积压过长缓存。
4. **DMA 6→3 备选预案**：新增 Kconfig `XIAOZHI_AUDIO_DMA_DESC_NUM`（默认 6）；若刷机实测打断仍有可闻微弱尾音，改 `sdkconfig.defaults` 加 `CONFIG_XIAOZHI_AUDIO_DMA_DESC_NUM=3` 后重跑 `build_fw_v111.ps1`。

### 2026-08-05 补充修正（第五轮：ASR 预连接 + 硬件防御）
**服务端（aliyunbl_stream.py + listenMessageHandler.py）**
- **ASR 预连接**：`listen:start` 时即建立 NLS 握手（`prewarm`），消除"说话时 ASR 还在连服务器"的 ~2s 窗口丢字。
- **5s 空转自动销毁**：预连后用户 5s 内未说话 → 自动关闭 ASR WebSocket，防空挂/扣费。
- **防重入原子锁**：`asyncio.Lock` 防止连续唤醒/双击重复拉起多个预连线程争抢音频流。
- **握手期 PCM 缓冲保留**：`conn.asr_audio`（13×60ms≈780ms）在 task-started 到达后冲刷，极速说话不丢首字。

**固件（v1.1-phase7.1 重建）**
- **干掉 `WaitForPlaybackQueueEmpty()` 阻塞等待**：打断后队列残留不再卡死麦克风推流启动（"聆听中但无音频上行"根因）。
- **开麦硬件防御**：`EnableVoiceProcessing` 后暖机 120ms（丢弃前 60ms PCM 电涌 Click + 30ms AFE 数字静音门槛），防喇叭尾音/回音导致自言自语误触发 VAD。
- **300ms 触屏防抖锁**：双击成功后 300ms 内忽略触屏事件，防电容屏抖动脉冲二次重入 `SetListeningMode`。
- **打断清屏**：`AbortSpeaking()` 调用 `display->ClearChatMessages()`，声音停止的同时清除滚动旧文本。

### 2026-08-05 补充修正（第六轮：异步解耦 + 崩溃防御 + 次生灾害防护）
- **异步解耦 SendStartListening**：新增 `MAIN_EVENT_SEND_START_LISTENING`，主循环异步发 listen 报文+开麦；`AbortSpeaking` 收敛为只清队列/标志/清屏，不再同步触发状态深链。
- **触屏 400ms 防踩踏锁**：双击/单击/滑动派发处均加锁；锁到期瞬间**排空触屏 IC 寄存器残留**（读一次丢弃 + 本地状态硬初始化为 RELEASED），防 401ms 后误读积压旧手势。
- **任务栈加固**：AFE 4096→8192、esp_timer 任务栈→8192（app_main 原已 8192）。
- **DRAM 监控**：boot 打印 `esp_get_free_internal_heap_size()`，要求 >40KB（防 Wi-Fi DMA OOM 断网）。
- **SendStartListening 断网重试**：异步处理时校验 `IsAudioChannelOpened()`，未连通限次重试（25 次），严禁静默吞包。

## 一、目标

| 维度 | 目标 |
|---|---|
| 播报打断 | 双击/触头瞬间切断播放（软刷新，禁跨线程 `i2s_channel_disable`） |
| 首字不丢 | 500ms 预录音（PSRAM 环形缓冲 + 瞬时首帧）+ 服务端回溯兜底 |
| VAD/ASR | 静默截断 500ms；NLS `status=20000000` 校验 |
| TTS | 首句流式分切 + Unicode 字数强制兜底（防首包死等） |
| LED | 打断/状态切换同步复位，杜绝卡蓝灯 |

## 二、固件改动（v1.1-phase7.1）

源码：`reference/stackchan-xiaozhi-firmware`（基于 v1.0.8-selfhost 基线）

### A. 瞬间打断（软刷新，零尾音）
- `audio_service`：新增 `AbortPlayback()`——置原子 `abort_requested_` + 清空 decode/playback 队列 + 重置 Opus 解码器。
- `AudioOutputTask`（唯一写 I2S 的任务）：检测到 abort 后丢弃队列剩余，**在同一任务内**写入 1~2 帧全 0 静音 PCM 冲刷 DMA（`FlushDmaWithSilence`），保持双工 RX（麦克风）BCLK 时钟，**不跨线程调用 `i2s_channel_disable`**（规避内核互斥死锁/看门狗）。
- `OpusCodecTask`：abort 期间丢弃解码包，防"停完又播"。
- `protocol`：`SendAbortSpeaking` 置 `audio_aborted_`，WS 下行二进制音频在打断后直接丢弃；`SetListeningMode`/`SendStartListening` 恢复接收。

### B. Opus 解码器真重置（审查点 #2 核实）
- 反汇编 `esp_audio_codec 2.4.1` 的 `libesp_audio_codec.a`：
  - `esp_opus_dec_open` 把 `opus_decoder_create()` 返回值存到句柄 **offset 0**（即 `struct[0] = OpusDecoder*`）；
  - `esp_opus_dec_reset` 加载 `struct[0]` 后调用 `opus_decoder_ctl(dec, 0x0FBC)`——**0x0FBC = 4028 = OPUS_RESET_STATE**。
- 结论：现有 `ResetDecoder()` 的 `esp_opus_dec_reset` 即真重置，无需额外 raw 句柄调用。已写入代码注释与本文档。

### C. 500ms 预录音（PSRAM + 瞬时首帧）
- `AudioService::Initialize`：`heap_caps_malloc(MALLOC_CAP_SPIRAM)` 分配 8000 samples（500ms @16k mono）环形缓冲，SPIRAM 不可用回退内部堆。
- AFE `OnOutput` 持续入环；本地 VAD（`OnVadStateChange`）检测到语音起点（`VAD_SPEECH`）时，把环内 500ms（含语音起始段）按 60ms Opus 帧**瞬时突发**送入编码队列——不做 30ms pacing（避免 250ms 墙上时间积压导致实时流永久滞后）。

### D. WS 接收缓冲 4096
- `websocket_protocol.cc`：`CreateWebSocket` 后调用 `SetReceiveBufferSize(4096)`（默认 2048，预卷首帧 ~3KB 大包会被截断）。

### E. Neopixel 卡蓝灯修复
- `SetDeviceState` 每次状态切换同步触发 `Led::OnStateChanged()`（此前只在 VAD 变化时触发）。
- `AbortSpeaking` 同步触发 LED 复位。
- `UpdateLedFromDeviceState`：离开活跃状态（speaking/listening/connecting）时**复位 `led_manual_`**并强制回到待机暖橙，不再恢复手动色导致卡蓝。

## 三、服务端改动

配置：`server/data/.config.yaml`
- `VAD.SileroVAD.min_silence_duration_ms: 500`（显式固化）。
- `ASR.AliyunBLStreamASR.max_sentence_silence: 500`（真实写入 NLS run-task `payload.parameters.max_sentence_silence`）。

代码（`server-patch/`，已挂载进容器）：
- `core/providers/asr/aliyunbl_stream.py`：
  - 打印实际发往阿里云 NLS 的 **run-task JSON**（核对判停参数确实透传）；
  - `task-started` 校验 `header.status_code == 20000000 Success`，非成功快速失败并打印完整 `error_code/error_message`；
  - 预卷缓存回溯发送时**逐帧 `await asyncio.sleep(0)`** 让出事件循环，防大包阻塞主循环。
- `core/providers/tts/base.py`（新增挂载）：`tts_one_sentence` 改为标点优先（`。！？!?；;，,、\n`，Unicode 切分）+ `MarkdownCleaner` 清理 + **24 字强制兜底**（无标点/Markdown 长句不再死等标点）；按 Unicode 字符切，绝不按字节（防截断 UTF-8 导致 EdgeTTS 400）。
- `core/providers/asr/base.py`：`conn.asr_audio` 缓存仅保留最近 800ms（13×60ms 帧）。

## 四、构建与刷机

- 构建：`firmware/build_fw_v111.ps1`（docker `espressif/idf:v5.5.2`，idf.py 自动拉取 `78__esp-wifi-connect`/`78__esp-ml307` 等组件）。
- 产物：`firmware/post-fw-v1.1-phase7.1/`（含 `merged-binary.bin` 一键烧录）。
- 烧录（app-only 或全量，见该目录 `README-flash.md`）：
  ```bash
  python -m esptool --chip esp32s3 -b 460800 --before default_reset --after hard_reset \
    write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m \
    0x0 bootloader.bin 0x8000 partition-table.bin 0xd000 ota_data_initial.bin \
    0x410000 xiaozhi.bin
  ```

## 五、真机验收清单

1. **打断**：播报长文时双击屏幕 → 立即无声（目标 <100ms 感知为瞬间），随后进入聆听蓝灯；无 Pop 爆音；不重复播报上一条。
2. **首字不丢**：唤醒后说话，识别文本开头完整（对比旧版前 1~2 字丢失）。
3. **延迟**：说完话到出结果 ≤0.6s（VAD 500ms + ASR 500ms + TTS 首句流式）。
4. **LED**：待机暖橙 / 聆听蓝 / 播报绿 / 连接金黄严格对应状态，打断后不卡蓝。
5. **麦克风**：打断后继续对话正常（RX BCLK 未受影响）。
6. **日志核对**：`docker logs xiaozhi-esp32-server` 出现 `ASR run-task JSON`（含 `max_sentence_silence: 500`）与 `task-started` 无 400 错误。

## 六、风险与未决

- DMA 残余尾音：约束禁止 `i2s_channel_disable`，软件冲刷后残余 ≤ DMA 深度（6×240 帧 ≈90ms 理论值，实际由队列清空+静音帧大幅压缩）；若真机仍可闻，下一步把 `AUDIO_CODEC_DMA_DESC_NUM` 6→3 并实测。
- 预卷效果依赖本地 AFE VAD 触发；若个别场景不触发，服务端回溯（`conn.asr_audio` 全量发送）兜底。
- 本版未改 `CONFIG_USE_DEVICE_AEC`/唤醒词灵敏度；如识别率仍差，属 Phase 7.2/后续范围。
