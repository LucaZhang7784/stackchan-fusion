# CHANGELOG v08.13（2026-08-13）

## 配网误触/误入根治（固件 reference/stackchan-xiaozhi-firmware-mqtt）

- **背景**：机器人反复出现"关机/重启后进入配网模式"。串口抓包定位到真凶：
  固件 `WifiBoard::TryWifiConnect` 启动 10 秒连接超时定时器，但 WifiStation
  组件要等 `IP_EVENT_STA_GOT_IP` 才上报 Connected（`WIFI_EVENT_STA_CONNECTED`
  处理器为空），board 才停掉定时器。家庭网络 DHCP 首个 OFFER 间歇性丢包/慢
  （实测 IP 到达 8~12 秒不等），10 秒窗口内未拿到 IP → 误判超时 → 自动进配网
  + 失败计数；旧固件连续 3 次失败还会**自动清空已存 SSID**，导致"每次开机必进
  配网"的恶性状态。
- **修复 1（连接超时 10s→20s）**：`wifi_board.cc` `CONNECT_TIMEOUT_SEC` 改为 20，
  覆盖 DHCP 首次 OFFER 丢包后的 lwIP 重试窗口（实测 8~12s 均落在窗口内），
  radio 已连上但 IP 未到不再被误杀。异地连不上时仍可开机单击屏幕立即进配网。
- **修复 2（进配网不再清空 SSID）**：`EnterWifiConfigMode` 移除
  `SsidManager::Clear()` —— 误入配网（开机单击/待机长按 5s）不再抹掉家庭 WiFi；
  配网成功经 `SsidManager::AddSsid` **追加**新网络，新旧 SSID 并存：
  异地配完网回家自动重连，去新地方也能照常进配网连其他 WiFi。
- **修复 3（移除 3 次失败自动清 SSID）**：`IncrementFailureCount` 不再累计失败后
  `Clear()`，偶发超时误进配网不再造成持久性丢配置。

## 真机验证（2026-08-13，串口连续复位）

- 修复前（10s 窗口）：复位 2/3 次出现 `WiFi connection timeout, entering config
  mode`，尽管 radio 已连上 DHSDWireless。
- 修复后（20s + 不清 SSID）：连续复位全部 `Got IP -> Connected -> idle`，零配网；
  IP 到达 8.3s / 11.4s，均落在窗口内。
- 保留行为：开机阶段单击触屏仍可进配网（供异地配网）；待机长按 5 秒仍可进配网。

## v08.14 修订（2026-08-13）：播报摘要只念"任务完成"丢内容 根治

- **背景**：机器人播报长文本时只念"项目完成/任务完成"框架词，不念内容与摘要。
  根因：`_summarize_for_speech` 把 `agent 任务完成: <长文>` 整体交给本地 LLM
  （qwen3.5:9b）做 ≤50 字摘要，且不校验输出——LLM 偶发退化输出框架词、改写编造
  内容，或吞掉"agent 任务完成:"前缀，网关照单全收进喇叭。
- **修复（gateway/fusion_gateway.py）**：
  1) **框架与正文分离**：摘要前拆出 `agent 任务完成/出错/需要确认: ` 前缀，
     只对正文做摘要，结束后强制拼回——框架永不丢；
  2) **LLM 输出退化校验**：新增 `_is_degenerate_summary`（过短 ≤2 字或命中黑名单
     "任务完成/已完成/好的/OK 等"）→ 判退化丢弃，改走尾部结论句提取兜底；
  3) **摘要 prompt 重新生成**：硬性要求必须包含原文具体内容与最终结论、保留关键
     数字与专有名词、严禁只输出无内容框架词。
- **真机验证（09:40:46 push ack）**：`codex 任务完成: 摘要修复验证任务完成，消息超长
  且含 EMQX 等关键数字专有名词及 QoS1，内容完整有效。`——前缀+内容结论+专有名词
  全部保留。
- **question 推/收闭环复验（09:41）**：推送 `claude 需要确认: 是否允许运行 git push
  到 GitHub 并部署到生产环境？` push ack；MQTT 模拟触屏回执 allow → `confirm ok`
  → `claude 已允许: ...` 结果播报 push ack。

## v08.14 修订 2（2026-08-13）：watcher 最终回复被"轮中评论"吞掉 根治

- **背景**：watcher 的 25 秒静默判定把轮中评论（如"打补丁："）当轮次完成提前播报，
  并按轮次标记已播；同轮最终回复随后被 `broadcast[turn]` 去重吞掉，用户只听到
  一句没头没尾的评论、真正的最终回复不进队列（09:36 播评论、09:42 最终回复丢失）。
- **修复（scripts/session_watcher.py）**：
  1) 去重从"按轮"改为"按 轮次+文本哈希"：同一轮文本变化（评论→最终回复）允许
     再播一次，只有完全相同内容才去重——最终回复必播；
  2) msg_uid 加文本哈希后缀（`watcher-<turn>-<hash8>`），避免网关 5 分钟幂等
     把重播吞掉（不同文本必然不同 msg_uid）；
  3) 保留新轮次触发 + 25 秒静默兜底。
- **单测**：同轮同文本去重、不同文本放行；msg_uid 随文本变化。

## v08.15（2026-08-13）：托盘性能优化 + UI 重设计 + 连接开关

### 1. 卡顿根治（gateway/fusion_tray.ps1 + 新增 gateway/tray_collector.ps1）

- **根因**：旧托盘把所有状态采集（healthz / docker mcp / WMI 进程 / 23MB 日志尾读）
  跑在 WinForms UI 线程，且每 5 秒全量重建菜单；慢操作（安装 Hooks、Hook 自检）
  还同步执行，点击后界面冻结数秒。
- **修复**：
  1) 状态采集移入**独立采集器进程**（tray_collector.ps1），每 5s 写
     `state/tray_status.json`；托盘 UI 只读该 JSON（≤20s 新鲜度），点击/开菜单即时响应；
  2) 菜单**仅在状态变化时重建**（签名比对），消除每 5 秒全量刷新卡顿与闪烁；
  3) 慢操作（连接开关 / Hook 自检 / 安装 Claude Hooks）改 **Start-Job 后台执行** +
     气球提示"运行中…"，完成后弹结果，UI 不再冻结；
  4) 单项提速：docker profile 30s 缓存；gateway.log 改文件流尾部 seek 读；
     修正 Get-CloudRobot 匹配 `push ok` → `push ack`（旧逻辑永远显示"暂无推送"）。

### 2. 界面优化（参考 Syncthing / Docker Desktop / Tailscale 托盘风格）

- 顶部状态头：`StackChan Fusion ● 已连接 [全部正常] HH:mm:ss`（彩色圆点 + 摘要）；
- 状态头正下方是**连接开关**（勾选态 ☑/☐，带提示）；
- 分组：Gateway / MCP / 机器人链路 / 播报队列 → 操作（队列操作 / Hook 自检 /
  安装 Claude Hooks / 重启网关）→ 退出；只读信息行带 ok/warn/bad 徽章配色。

### 3. 本机 ⇄ 机器人 连接开关（多机共用同一配置）

- 网关新增 `POST /api/robot_attach {"attached": bool}`，持久化到
  `state/robot_attached.json`（重启保持），`healthz` 返回 `attached`；
- `_push_worker` / `_drain_pending` 门禁：**断开时消息照常入 pending 队列但不推
  MQTT**，连接后 5s 内 `_push_loop` 自动补推（不丢消息、不双机抢播）；
- 托盘开关点击即调接口；图标/状态头/提示同步显示已连接/已断开；
- 真机验证（18:35）：断开→测试消息 8s 仍停留 pending 未推；连接→7s 内补推
  `push ack [agent]`。

### 已知取舍

- 采集器为独立进程（规避 PS 5.1 线程池无 Runspace 的崩溃）；托盘退出时按
  `state/tray_collector.pid` 停止采集器。

## v08.16（2026-08-13）：托盘图标状态映射 + 机器人探活 + 开关即时刷新

### 1. 图标状态映射（机器人离线=红，MCP/Hook 故障=黄）

- 采集器新增 `robotOnline`（机器人本体在线判定）与 `hookFault`（Hook 自检异常判定），
  状态优先级：网关离线/机器人离线 → 红；MCP 异常或 Hook 异常 → 黄；全好 → 绿。
- 模拟验证：注入 `hook_health.last.txt=存在异常` → `state=warn`（黄）；恢复后回 `ok`。

### 2. 机器人静默探活（gateway/fusion_gateway.py）

- 新增 `_robot_ping_loop`：本机已连接时每 5 分钟发一次静默 ping
  （START action=ping 空文本 + STOP，走 worker 队列串行不打断播报），
  固件收到 START 即回 ACK → 日志 `robot ping ack/no-ack`；
- 采集器据最近一次探活/播报结果判定机器人本体在线/离线（无记录默认在线）；
- 实测：`_robot_ping_send` 返回 ack=True（机器人在线）。

### 3. 连接开关即时刷新（gateway/fusion_tray.ps1）

- 点击开关成功后 `attachOverride` 本地立即生效 + 强制下个 UI 周期重建菜单
  （不再等最长 5 秒采集周期）；采集器确认后自动清除覆盖；
- 机器人链路子菜单新增：`机器人本体: 在线/离线`、`本机连接: 已连接/已断开`、
  `Hook 自检: 正常/存在异常` —— 断开本机连接时机器人本体状态仍清晰可见。

## v08.17（2026-08-15）：托盘一键重启所有服务 + 状态刷新周期 20 秒

- **一键重启激活所有服务**（gateway/fusion_tray.ps1）：操作区新增
  「重启激活所有服务」菜单项——后台 job 依次：停止/启动网关（stop/run_gateway）、
  停止/启动云桥 MCP（stop/run_bridge）、杀旧 Funnel 进程并启动
  （start_funnel_proxy），最后轮询 healthz 确认网关恢复健康，弹窗汇总结果。
- **stop_gateway.ps1 加固**：原实现只按 pid 文件杀进程，pid 文件过期时网关
  实际杀不掉（08-15 实测旧 PID 49044 原样存活）；现一律按 8010 端口兜底停止
  真实网关进程，保证一键重启可靠生效。
- **状态刷新 20 秒**：tray_collector 写入周期 5s→20s，托盘 UI 状态定时器 2s→20s
  （读缓存，状态变化才重建菜单）；忙任务（慢操作/一键重启）用独立 1s 定时器轮询，
  完成弹窗不受 20s 刷新周期影响；Get-CurrentStatus 新鲜度阈值 20s→60s 防误判。
- **真机验证（08-15）**：一键重启序列实测——网关 49044→44848 重启成功、云桥与
  Funnel 均拉起、healthz 恢复 ok/attached=True；状态 JSON 刷新间隔实测 20.8s。

## v08.18（2026-08-15）：agent 事件同步弹 Windows 系统通知

- **新增 scripts/notify_windows.ps1**：Windows 通知助手——优先现代 Toast
  （WinRT + Start Menu 快捷方式自注册 AppUserModel.ID `StackChan.Fusion.Gateway`），
  失败自动回退系统托盘气泡；带 5s 渲染保活与 `state/notify_windows.log` 落盘。
- **网关集成（gateway/fusion_gateway.py）**：`/api/agent_event` 与触屏确认回执处
  调用 `_notify_windows`（后台 Popen 异步，不阻塞网关，失败静默）：
  - `done` → `X 任务完成: <摘要>`
  - `error` → `X 出错: <摘要>`
  - `question` → `X 需要确认: <内容>`
  - 确认回执 → `X 已允许/已拒绝: <问题>`
- 覆盖全部 agent（codex / claude / agy / vscode / system），所有事件都经同一入口。
- **真机验证（08-15）**：done/question/confirm 三类均弹出 Windows 通知
  （notify_windows.log 留痕），机器人播报同步正常。

## v08.19（2026-08-15）：播报时长阈值改为 15 秒

- **规则变更**（gateway/fusion_gateway.py）：原先按"50 字"阈值摘要，改为按
  **语音时长**判定——估算时长 ≤15 秒完整播报；>15 秒用本地 LLM 提炼为约 15 秒的
  口语化摘要（框架"agent 任务完成/出错/需要确认"永远保留，摘要含完整结论，
  退化输出丢弃改尾部提取）。
- **时长估算**：新增 `_speech_duration_s`（字符数 / 语速），语速默认 4.5 字/秒，
  可用 `config.json` 的 `push_tts_cps` 调整（config.json.example 已加）。
- 覆盖全部播报类型：完成 done / 提问 question / 待确认（question 触发）/ 报错 error
  （含确认回执 已允许/已拒绝）。
- **真机验证（08-15）**：短消息（28 字 ≈6s）完整播报；超长消息摘要 67 字 ≈14.9s，
  关键信息（EMQX/QoS1/G 盘/87MB/粤语女声）全部保留。

## v08.20（2026-08-15）：接入 DeepSeek Harness（dsh）

- **背景**：本机已安装 DeepSeek 官方开源 agent harness（`C:\Users\zhang.luca\deepseek-harness`，
  v0.1.0-rc.5，Web UI 于 127.0.0.1:3080）。调研确认：有无头 CLI
  （`dsh --profile headless "任务"`，stdout 输出最终助手文本）；无桌面 hooks 体系；
  会话以 zstd 压缩事件流存于 `~/.dsh/sessions/**/session.jsonl.zstd`。
- **机器人语音派发（gateway/agents_core.py）**：注册 `deepseek` agent
  （cli=pnpm dsh --profile headless，workdir=~/deepseek-harness）+ 可见窗口
  `DeepSeek-Asong`；别名 deepseek/dsh/深寻/深度求索。机器人说"让 deepseek 做 X" →
  headless 执行 → 结果经 MCP 语音返回。实测 `只回复四个字: 测试成功` → "测试成功"。
- **Web 会话兜底播报（新增 scripts/dsh_session_watcher.py）**：zstd 流式解码，
  提取每轮最终 assistant/message 文本，`turn/end` 时上报 done → 机器人播报 +
  Windows 通知；跳过 cwd=deepseek-harness 的 headless 会话（结果已走 MCP 语音，
  防双播）；按 轮次+文本哈希 去重（msg_uid=dshwatch-<session8>-<turn>-<hash8>）。
- **网关集成**：新增 `_dsh_watcher_loop` 后台线程（20s 周期）；
  agent_event 白名单按 AGENT_CLIS 自动放行 deepseek（done/error/question 全部触发
  播报+通知）。依赖 `zstandard` 已加入 requirements.txt。
- **真机验证（08-15）**：合成 Web 会话 turn/end → `posted test-a13 turn=1` →
  `push ack [agent]: deepseek 任务完成: …` + Windows 通知；headless 会话正确跳过。

## v08.21（2026-08-15）：托盘忙任务修复 + 云桥 python 加固

- **托盘 ShowBalloonTip 重载修复（gateway/fusion_tray.ps1）**：4 参数
  `ShowBalloonTip(timeout,title,text,icon)` 在 PS 5.1 运行时绑定失败
  （`Cannot find an overload... argument count: 4`），导致所有忙任务
  （Hook 自检、安装 Claude Hooks、重启激活所有服务）Start-BusyJob 直接失败、
  点击无反应。改为先设 `BalloonTipTitle/BalloonTipText/BalloonTipIcon` 属性，
  再调 1 参数 `ShowBalloonTip(timeout)`，三处调用全部修正。
- **云桥 python 加固（xiaozhi-mcp/run_bridge.ps1）**：`Get-Command python` 可能
  解析到缺 websockets 的解释器（实测 mcp_pipe 报 ModuleNotFoundError，桥守护拉起
  反复失败）；改为显式使用 Python311 路径（与 run_gateway.ps1 一致），缺失时回退。
- **真机验证（08-15）**：桥恢复（进程 54152+48684，robotOk=True）；
  tray_status state=ok / robotOnline=True / mcpOk=True / attached=True；
  tray_err.log 无新 ShowBalloonTip 错误。

## v08.22（2026-08-17）：屏幕提示"主人/小智"→"你/阿松"

- **背景**：机器人屏幕仍显示"小智/主人"（如"主人勾着小智的脖子"）——云端 LLM
  偶发用"小智"自称、称用户"主人"。
- **固件屏幕归一化（m5stack_core_s3.cc SetChatMessage）**：屏幕显示路径统一过滤
  `小智→阿松`、`主人→你`——云端回复、触摸旁白、系统提示全部上屏前替换，
  屏幕永远显示"阿松/你"（音频仍由云端输出，如需同步改请在 xiaozhi.me 控制台
  更新阿松 System Prompt）。
- **触摸动作旁白**：7 条"（主人摸了摸小智的头）…"改为"（你摸了摸阿松的头）…"。
- **提示词副本**：stackchan_prompt.txt 中"主人"→"你"、"史塔克酱"→"阿松"。
- **交付**：重建固件 + app-only 刷 0x410000（保留配置）；机器人在线验证
  （robot ping ack=True，tray_status 全部正常）。

## v08.23（2026-08-17）：阿松 System Prompt v4.0（称呼铁律 + 15 秒播报对齐）

- **背景**：屏幕层已固件归一化"小智→阿松、主人→你"；但**语音**仍由云端 LLM
  输出，偶发自称"小智"、称用户"主人"。需在 System Prompt 层加硬约束。
- **新增 prompt-阿松-v4.md**（v4.0）：
  - **称呼与自称铁律**：自称永远"阿松"、称呼用户永远"你"；严禁"小智/XiaoZhi/
    主人/老板"；文本中出现一律改写后再开口；
  - **播报 15 秒对齐**：LLM 回复 ≤60 字（约 15 秒），超长先自行摘要；
  - 沿用 v3.6 全部保固：LED 归固件、手势不发文本、ASR 容错、工具调用铁律
    （新增 deepseek/深寻/深度求索 识别）、拍照铁律、粤语女声。
- **交付**：把 v4.0 全文粘贴到 xiaozhi.me 控制台 → STACK 智能体 → 系统提示词，
  语音与屏幕即统一为"阿松/你"。

## v08.24（2026-08-17）：播报严格按屏幕字幕（全文）播报

- **需求**：播报长度此前远少于字幕内容——v08.19 的 15 秒摘要把长消息压成约 60 字。
- **修改（gateway/fusion_gateway.py push_send）**：新增配置 `push_full_broadcast`
  （config.json，默认 `true`）——为 true 时**不做 15 秒摘要，字幕与语音都发完整原文**
  （播报=字幕=全文）；设 false 则回到 v08.19 的 15 秒摘要规则。config.json.example
  已同步。
- **真机验证（08-17）**：约 200 字长消息全量播报（此前 15 秒摘要版仅 61 字），
  字幕与语音严格一致。
- **云端侧**：确认阿松 Prompt v4.0 已贴回 xiaozhi.me 控制台（回复 ≤60 字，
  云端 TTS 不掐尾）。

## v08.25（2026-08-17）：队列消息卡死根治 + 托盘乱码修复 + 忙任务气球修复

- **队列卡死（gateway/fusion_gateway.py）**：英文为主的长文本被 `len×200ms` 估时
  高估，EdgeTTS 正常输出被误判"截断"→ 回退粤语 sherpa-onnx → 该模型无法分词
  英文导致 Conv 节点崩溃 → push fail → 消息卡在 pending 每 30s 重试。
  修复：① 估时改中英文分别计价（中文≈230ms/字、英文≈90ms/字），截断阈值 55%→40%；
  ② 英文为主文本不再走 sherpa（跳过必崩路径），最后一次 EdgeTTS 直出（音频有效即用）。
  实测：403 风格英文长消息全量播报 push ack。
- **托盘乱码（fusion_tray.ps1 / tray_collector.ps1）**：队列/事件/确认文件读取
  未指定 `-Encoding UTF8`，PS 5.1 按 GBK 读 UTF-8 显示"浠诲姟瀹屾垚"等乱码；
  所有 JSON/JSONL 读取统一补 `-Encoding UTF8`（数据本身完好，纯显示修复）。
- **忙任务气球（fusion_tray.ps1）**：`BalloonTipIcon = ToolTipIcon::Information`
  枚举名错误（正确为 `Info`）→ "重启激活所有服务"等忙任务自 v08.21 起一直失败；
  修正为 `::Info`。
- **真机验证（08-17 16:25）**：英文长消息 push ack；tray_status 全部正常；
  tray_err.log 无新气球错误；pending UTF-8 显示无乱码。

## v08.26（2026-08-17）：托盘播报历史队列

- **需求**：托盘可查看"已播报消息"的历史记录，区分已送达 / 离线未确认 / 推送失败。
- **网关（gateway/fusion_gateway.py）**：新增 `_broadcast_history_append`，
  在 `_push_worker` 的 ack / no-ack / fail 三处结果落盘
  `gateway/state/broadcast_history.jsonl`（`{"ts","status","source","text"}`），
  保留尾部 500 条（每 100 条裁剪一次，避免频繁整文件重写）；ping 探活静默，
  不进历史。
- **采集器（gateway/tray_collector.ps1）**：读取历史尾部，tray_status.json 新增
  `histCount` / `lastBroadcast` / `lastBroadcastTs` / `broadcastRecent`（最近 5 条），
  状态详情新增「播报历史」行。
- **托盘（gateway/fusion_tray.ps1）**：机器人链路新增「最近播报」行；播报队列新增
  「播报历史: N 条」；队列操作新增「显示播报历史...」弹窗（最近 50 条倒序，
  UTF-8 读取，状态中文映射 已播报/未确认/失败）。
- **验证（08-17 19:33）**：测试推送 `codex 任务完成: 播报历史队列功能测试`
  → push ack → broadcast_history.jsonl 落盘 → tray_status histCount/lastBroadcast
  /broadcastRecent 正确刷新；托盘 UI 可查看历史。

## v08.27（2026-08-17）：托盘菜单"全服务离线"假象根治

- **现象**：采集器 tray_status.json 一切正常（state=ok / 网关在线 / 机器人在线），
  图标与气泡也正常，但托盘菜单恒显"Gateway 离线 / MCP 异常 / 机器人离线"。
- **根因（fusion_tray.ps1）**：`Build-Menu` 开头读取 `$script:status` 缓存，
  但实时刷新路径 `Update-Status` 只把新鲜快照用于图标/气泡，从未写回
  `$script:status`（唯一写它的 `Update-StatusCache` 是死代码，无人调用）——
  菜单永远渲染启动时的初始占位表（全 false）。这也解释了此前
  "图标绿色但详情全离线" 的旧报告。
- **修复**：`Update-Status` 取到最新 `$s` 后补一行 `$script:status = $s`，
  菜单即渲染真实快照。
- **顺手修正（tray_collector.ps1）**：`Get-BroadcastHistory` 返回首元素
  `,$hist.Count` 的前导逗号把 histCount 包成单元素数组，去掉后为标量。
- **验证（08-17 19:44）**：托盘重启后 state=ok / gwOk=true / mcpOk=true /
  robotOnline=true；histCount=12（Int64 标量）；最近播报正常显示。

## v08.28（2026-08-17）：屏幕归一化 UTF-8 字节替换 bug 根治（"你人/阿松智"）

- **现象**：屏幕提示把"主人"显示成"你人"、把"小智"显示成"阿松智"。
- **根因（m5stack_core_s3.cc SetChatMessage）**：`std::string::find` 返回字节
  偏移，UTF-8 中文每字 3 字节（"小智"/"主人"各 6 字节），但替换用了
  `replace(pos, 2, ...)` 只删 2 字节——残留 1 个坏字节 + 后一个字，屏幕吞掉
  无效字节后显示"阿松智 / 你人"。
- **修复**：删除长度改为 6 字节（整词替换）：`replace(pos, 6, "阿松")` /
  `replace(pos, 6, "你")`。
- **交付**：重建固件 + app-only 刷 0x410000（保留配置）。

## v08.29（2026-08-20）：Antigravity hooks.json promlight 嵌套结构拒载自愈

- **现象**：托盘提示"组件异常"。自检报 `Antigravity: language_server 拒载
  hooks.json(Failed to parse)`。
- **根因（~/.gemini/config/hooks.json）**：`promlight` 段仍为旧版嵌套结构
  `{"hooks":[...]}`，Antigravity Go 加载器（hooks.go:44）要求条目顶层直接带
  `command/type/timeout`，嵌套条目触发整文件拒载
  （`invalid hook "promlight": command hook must specify 'command'`）——
  stackchan 段本身是好的，但整文件被拒导致所有 Antigravity 钩子失效。
  此前 hook_health 自动修复只重建 stackchan 段、保留其它节点，无法消除此异常。
- **修复（scripts/hook_health.py）**：新增 `_normalize_legacy_namespaces()`，
  自检时先把非 stackchan 命名空间（如 promlight）的旧嵌套条目扁平化为平命令
  对象（保留命令与其它自定义节点，强备份后写入），再校验 stackchan 段——
  同类损坏今后可自愈。
- **人工修复**：运行 hook_health 已自动备份
  `hooks.json.bak-auto-repair-20260820-114121` 并扁平化 promlight 段；
  需重启 Antigravity 桌面使新配置加载生效。

## v08.30（2026-08-20）：心跳告警"旧会话残留"防误杀

- **现象**：v08.29 修复并重启 Antigravity 后，自检仍报
  `钩子未触发(antigravity_hook.log 已 69h 无写入, 但 X 分钟前有真实会话活动)`——
  该活动是配置修复**之前**的旧会话（当时钩子确实失效），属于残留误报。
- **修复（scripts/hook_health.py antigravity_heartbeat）**：当最近一次
  transcript 活动早于 hooks.json 最近一次修复/写入时间时，判定为"旧会话残留"
  直接跳过告警；修复后若仍有新会话却不写入钩子日志，依然正常告警。
- **验证（08-20 11:47）**：自检 `全部正常`，托盘 state=ok / hookFault=False。

## v08.31（2026-08-31）：机器人 MQTT 在线状态闭环与单实例保固

- **根因复盘**：机器人重启时，Wi-Fi、云端激活和第二条 Push MQTT 订阅之间存在启动窗口；
  原托盘只能等一次播报 ACK 才判断在线，容易把“重连中”误标为离线。与此同时，托盘与
  重启入口并发启动网关/云桥，曾出现 8010 端口占用与过期 PID 快照。
- **固件（m5stack_core_s3.cc）**：第二条 MQTT 在连接后向
  `stackchan/{mac}/status` 发布 retained `online`；配置 QoS1 retained 遗嘱 `offline`，
  让 broker 在异常断连后自动更新状态。
- **网关（gateway/fusion_gateway.py）**：启动即订阅 status 主题，healthz 返回
  `robot_presence` 与更新时间；机器人变 online 时立即尝试恢复保留的 pending 队列。
- **托盘（tray_collector.ps1）**：优先使用 MQTT status，显示“在线 / 重连中 / 离线”；
  对未刷新固件暂时兼容最近 ACK 回退判定。
- **单实例（run_gateway.ps1 / run_bridge.ps1 / restart_gateway.ps1）**：新增 Windows
  命名启动锁；网关在 5 秒内确认真实绑定 8010 后才记录 PID；托盘“重启网关”统一走
  stop-wait-start 脚本，避免端口竞态。
- **COM 隔离（latency_check/capture_serial.py）**：串口诊断只接受
  COM8 且校验 `VID_303A:PID_1001`；传入 COM3 会明确拒绝。
- **验收**：Python/PowerShell 语法校验通过；固件重新编译并仅刷入 COM8 的
  `0x410000` 应用分区（Hash verified）；新固件串口确认订阅 push 与发布 status online；
  独立 QoS1 silent ping 收到匹配 ACK；保留队列随后恢复 ACK 清理。
