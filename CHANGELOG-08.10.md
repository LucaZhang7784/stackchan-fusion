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

## v08.10.2（2026-08-10 深夜）

- **Antigravity hooks 根因定案并修复**：Antigravity 桌面端由 Go 语言服务器
  （language_server.exe / jsonhook.go）解析 `~/.gemini/config/hooks.json`，**只认扁平
  命令对象**（条目顶层直接带 `command`）；嵌套 `{"hooks":[...]}` 会被拒载
  （hooks.go:44 "command hook must specify 'command'"）。已按扁平结构重写，真机确认
  `posted done ok` + 机器人播报恢复。
- **hook_health.py 大升级（保固 v2）**：
  - Antigravity 校验/修复标准改为扁平结构（检测到嵌套 → 备份 `.bak-auto-repair-*`
    后重建扁平模板，仅动 stackchan 段、保留其它自定义节点）；
  - 新增 Antigravity loader 真实状态自检（尾部读取 language_server.log，比较
    Loaded vs Failed to parse，防全量扫描大日志）；
  - 新增钩子心跳检测（防误杀：仅当 Antigravity 在运行 + loader 近 60 分钟有活动、
    但 antigravity_hook.log 超 6h 无写入才告警；隔夜挂机不告警）；
  - 告警文案带具体原因（格式被改坏 / loader 拒载 / 钩子未触发）。
- **固件状态机加固（语音字幕不一致根治）**：STOP 后启动 200ms 极速轮询，播放队列
  排空 + 100ms DMA 放空后自动切回 Idle（清字幕 / 回暖橙 / 恢复唤醒）；状态防踩踏
  （仅 Speaking 才切 Idle，进 Listening 立即清标志）；重建刷机 0x410000。
- **网关 EdgeTTS 截断防护**：合成时长 < 期望（字数×200ms）的 55% 视为中途断流，
  自动重试一次，仍失败回退本地 sherpa（同文本，内容一致）。
- **文档保固**：README/MEMORY 新增 Hooks 保固规范（Antigravity=扁平、Claude/Codex=嵌套、
  唯一修复方 hook_health），防再次被"修"错。

## v08.10.3（2026-08-11 上午）

- **心跳误报修复**：`antigravity_heartbeat` 的"交互信号"从 language_server.log 修改时间
  改为 **brain 会话 transcript 更新时间**（language_server.log 会被后台 CDP/权限日志持续
  touch，不能当交互信号）；并加"会话仍在写入（3 分钟内）则不告警"。实测挂机 12h 不再误报。
- **自检周期 30 分钟 → 5 分钟**：更快发现配置漂移/钩子未触发；周期检查改用
  `--alert --check-only`（不再每 5 分钟刷 4 条 progress 事件污染队列视图；
  链路自检由托盘「Hook 自检与修复」手动执行）。
