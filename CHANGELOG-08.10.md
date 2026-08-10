# CHANGELOG v08.10（2026-08-10）

## 播报体验优化（gateway/fusion_gateway.py）

- **播报规则**：≤50 字消息完整播报；>50 字经本地 LLM（Ollama qwen3.5:9b，回退
  qwen3:8b / gemma4:12b）口语化摘要为 ≤50 字，LLM 不可用或超时降级截断（推流永不卡死）。
  先只清洗原文（去 markdown / 压空白）不预截断，保证摘要器拿到完整原文。
- **语速归 1.0x**：EdgeTTS `push_tts_rate` +20% → +0%；本地兜底 sherpa-onnx
  `tts_fallback_speed` 1.1 → 1.0（config.json 与 config.json.example 同步）。
- **QoS0 → QoS1（吞字根治）**：`push_send` 的 START / 音频批 / STOP 全部改 QoS1；
  固件订阅本为 QoS1，公网 EMQX 丢包或瞬时断连时 broker 重投，消灭 QoS0 静默丢帧
  导致的播报吞字（ACK 只证明 START 到达，音频帧必须靠 QoS1 保送达）。

## 语音识别基准确认（排查记录，无代码改动）

- 云链路（xiaozhi.me）：识别基准 = 云端 ASR 语种设置；固件只本地做唤醒词「阿松」，
  不下发任何语言参数。
- selfhost 兜底（aliyunbl_stream）：容器 config.yaml 中 `language_hints` 与
  `vocabulary_id` 均为注释态（自动语种检测）；8/5 曾实证中文被误听为日语
  「あ、そうな」。如需中英混合识别，需打开 `language_hints: ["zh","en"]` 并配热词。

## Trae 桌面端接入研究（docs/trae-work-integration-feasibility-20260807.md）

- 结论：本机 TRAE Work CN 桌面端原生内置完整 TraeCode hooks 引擎（ai-agent 原生模块，
  含 Stop / Notification / PreToolUse 事件），**不需要企业版账号**（企业版限制仅
  TraeCode CLI 的 ToB 登录）。
- 全局 hooks 路径确认为 `%USERPROFILE%/.trae-cn/hooks.json`
  （product.json `dataFolderName = .trae-cn`）。
- 方案：trae_hook.py（done / question / progress 回流）+ hooks.json + 网关 trae 注册；
  待批准实施。
