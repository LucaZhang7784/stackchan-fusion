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
