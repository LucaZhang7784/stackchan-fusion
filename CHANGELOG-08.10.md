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

## v08.10 修订（2026-08-10 晚）

- **摘要保留完整结论**：`_summarize_for_speech` 提示词强制"必须包含完整结论"
  （根因/结果/决定），可省略过程细节；LLM 不可用/超时降级从头部硬截断改为
  **尾部结论句提取**（`_conclusion_fallback`），避免"只说开头、丢了结论"。
- **Hook/Bridge 强壮性加固**：
  - 新增 `scripts/hook_health.py`：校验并自动修复 Antigravity（嵌套结构模板重建）/
    Claude（重跑安装脚本）/ Codex（补齐缺失事件）hooks 配置 + 链路自检
    （progress 不播报）+ `--alert` 机器人告警；
  - 托盘新增「Hook 自检与修复」菜单项；
  - 网关内置 30 分钟周期自检线程（启动 60s 首检），异常自动修复并以
    `agent=system` 推送告警（msg_uid 去重）；注册 `system` agent（agent_event 白名单）；
  - 确认托盘 `Restore-BridgeIfDown` 已自动拉起 xiaozhi-mcp 云桥接（无需新增）。
- **固件 v1.2-mqttpush 修订（重建刷机 0x410000，保留配置）**：
  - push MQTT 会话持久化 `cfg.session.disable_clean_session = true`：断连期间 QoS1 帧
    由 broker 缓存、重连补投（根治中途断音）；
  - 断流看门狗 3s → 5s，给重连补投留时间窗；
  - 真机验证：刷机后测试播报 push ack 成功。
