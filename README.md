# StackChan 融合方案

让 **StackChan 桌面机器人**（M5Stack CoreS3）通过语音指挥本机的
**codex / claude / agy / pi / vscode** AI agent：查询状态、派发任务、播报结果、语音确认。

> v08.16（2026-08-13）：托盘图标状态映射（机器人离线=红 / MCP/Hook 故障=黄）+
> 机器人 5 分钟静默探活（本体在线判定）+ 连接开关即时刷新 + 菜单显示机器人本体/
> 本机连接/Hook 自检三项状态。
> v08.15（2026-08-13）：托盘卡顿根治（后台采集器 + 状态缓存 + 慢操作后台化）+
> 托盘 UI 重设计（状态头/分组/徽章）+ 本机⇄机器人连接开关（/api/robot_attach，
> 多机共用配置时只一台推流，断开消息入队、连接后自动补推）。
> v08.14 修订2（2026-08-13）：watcher 最终回复必播——去重改"轮次+文本哈希"（评论
> 抢跑不再吞最终回复）+ msg_uid 带文本哈希（网关幂等不误杀重播）。
> v08.14 修订（2026-08-13）：播报摘要根治——框架与正文分离（"agent 任务完成:" 永不丢）
> + LLM 退化输出校验（只回"任务完成"丢弃，改尾部结论提取）+ 摘要 prompt 强制含内容
> 结论与专有名词；question 推/收闭环复验通过。
> v08.13（2026-08-13）：配网误入根治——连接超时 10s→20s（覆盖间歇性 DHCP 慢/丢包，
> radio 已连上不再被误杀进配网）+ 进配网不再清空 SSID + 移除 3 次失败自动清空；
> 异地仍可开机单击/长按进配网，新旧 WiFi 并存自动重连。
> v08.12（2026-08-12）：Codex 回复必播兜底（session_watcher 直读 transcript，续传会话
> hooks 失效也必播）+ 确认/权限请求播报补全（AskUserQuestion/ExitPlanMode 念具体内容）+
> 句尾 240ms 静音尾帧（吞字根治）+ watcher 心跳纳入 Hook 自检保固。
> v08.10（2026-08-10）：播报规则 ≤50 字完整 / 超长 LLM 摘要 ≤50 字 + 语速归 1.0x +
> 推送 QoS0→QoS1（公网丢帧吞字根治）；Trae 桌面端 hooks 接入研究（原生引擎，免企业账号）。
> 修订：摘要保留完整结论 + Hook 自检与修复（托盘菜单 / 30 分钟周期 + 机器人告警）+
> 固件 MQTT 持久会话与看门狗 5s。
> v08.08（2026-08-07 午后）：LED 灯环根治（PY32 GPIO13 出厂序列 + led_manual_ 待机锁 +
> I2C 互斥；真机播报绿→待机暖橙）+ Antigravity 播报修复（hooks 命名空间 `stackchan` +
> AfterAgent/agent.stop 事件别名）+ Prompt v3.6（灯色归固件）+ 固件 v1.2-mqttpush 重建。
> v08.09（2026-08-07 晚）：Claude hooks Windows 失效根治（settings.local.json → settings.json，
> 规避 anthropics/claude-code#64699）+ 安装脚本 PowerShell 5.1 兼容重写 + 托盘
> 「安装/修复 Claude Hooks」一键自愈菜单 + 网关两轨修复（MQTT 2帧/批根治 TCP 分片丢包吞字 +
> LLM ≤60 字口语化摘要）。
> v08.07（2026-08-07）：Phase 8 动作联动（done→点头 / question→歪头）+ CoreS3 视觉
> `robot_snap` 拍照 MCP（12 工具）+ Claude hooks 迁移 `settings.local.json` 抗覆盖 +
> VS Code 语音派发拒发 + Claude 流式中断兜底。

> 核心思路（2026-08-06 收尾）：**云链路 + MQTT 主动播报**。机器人走 xiaozhi.me 云端智能体，
> agent 事件经网关 FIFO 队列 → EdgeTTS(粤语) → µ-law → EMQX MQTT 直推固件播报，
> **msg_uid 全链路幂等 + 固件 ACK 点杀**（播报不重不漏）；任务在 agent 自己的可见窗口执行，
> 结果经 hooks 回流。自建 xiaozhi-esp32-server 链路已停用。

## 架构

```
机器人 (M5Stack CoreS3, 固件 v1.2-mqttpush, 唤醒词「阿松」)
   │ 语音 (ASR/LLM/TTS 在 xiaozhi.me 云端)
   ▼
xiaozhi.me 云智能体 (STACK, 提示词见 prompt-阿松-v3.md)
   │ MCP (wss://api.xiaozhi.me/mcp)
   ▼
xiaozhi-mcp 云桥接 (mcp_pipe.py + server.py, 本机)
   │ agent_status / agent_query / agent_pending / agent_confirm / agent_result_check / robot_snap ...
   ▼
融合网关 fusion_gateway.py (:8010, Bearer 认证)
   │
   ├── 播报链路: 单 Worker FIFO → EdgeTTS(粤语) → µ-law → EMQX MQTT
   │            (stackchan/{mac}/push, START 带 msg_uid, 固件 ACK 后点杀)
   ├── codex   (Stop hook 带 msg_uid → 网关幂等入队)
   ├── claude  (Stop/SessionEnd/PermissionRequest hook + confirm_mcp 确认回环)
   ├── agy     (Antigravity fusion hooks)
   ├── pi      (扩展 hooks-bridge.ts)
   └── vscode  (vscode_hook.py, VS Code 任务完成上报)
        └── 机器人任务 → agent 自己的可见窗口执行 (Codex-Asong / ClaudeCode-Asong / ...)
```

两条链路：

| 链路 | 说明 |
|---|---|
| 云链路（主） | 机器人语音走 xiaozhi.me；agent 事件经网关 → EMQX MQTT µ-law 主动播报 |
| 自建链路 | **已停用**（容器 Exited；播报不再依赖 xiaozhi-esp32-server） |

## 功能

| 能力 | 说明 |
|---|---|
| 主动播报 | agent 完成/出错/需确认 → 网关立即推送（≤60 字 LLM 口语化摘要，长文本 LLM 提炼、失败降级截断），msg_uid + ACK 点杀不重不漏 |
| 唤醒补播 | 机器人离线时消息保留 pending 队列，唤醒后 `agent_pending` 补播 |
| 状态查询 | 「检查 XX 状态」→ `agent_status`（4 个 agent 可用性/进程/最近事件，<5s） |
| 任务执行 | 「让 XX 做…」→ `agent_query`，在 agent 自己的可见窗口执行，结果回流播报 |
| 确认回环 | claude 权限请求 → 机器人念问题 → 语音回答 → 回写 allow/deny（claude 完整支持） |
| 设备控制 | 点头/摇头/转向/表情/拍照/LED（固件自动跟随状态灯） |

## 快速开始

### 新电脑 / 新机器人

完整部署步骤（含全部占位符配置、刷固件、配网、xiaozhi.me 绑定、四 agent hooks）见 **[DEPLOY.md](DEPLOY.md)**。

### 本机服务

```powershell
# 融合网关 (:8010, 必须)
powershell -ExecutionPolicy Bypass -File gateway\run_gateway.ps1
# 云桥接 (机器人走 xiaozhi.me 时, 必须)
powershell -ExecutionPolicy Bypass -File xiaozhi-mcp\run_bridge.ps1
# 备用链路容器 (可选)
docker compose -f server\docker-compose.fusion.yml up -d
# 托盘 + 自启 (可选)
powershell -ExecutionPolicy Bypass -File gateway\install_autostart.ps1
```

### 验证

```powershell
python scripts\verify_connectivity.py
```

全部 PASS 后：对机器人说「阿松」唤醒 → 应自动播报待办；说「检查 agent 状态」→ 播报四 agent；
说「让 codex 总结项目」→ 桌面弹出 Codex 窗口执行 → 完成后唤醒机器人听结果。

## 四 Agent 接入

| Agent | 接入方式 | 主动上报 | 语音回写确认 |
|---|---|---|---|
| codex | `~/.codex/hooks.json` → `agents/codex_hook.py`；`config.toml` `bypass_hook_trust=true`、`[windows] sandbox='unelevated'` | ✅ 桌面+CLI | ❌（在 codex 界面确认） |
| claude | `~/.claude/settings.json` hooks → `agents/claude_hook.py`（v08.09 起主存 settings.json——Windows 2.1.x 的 settings.local.json 有 #64699 静默失效 BUG；ccswitch 覆盖后托盘「安装/修复 Claude Hooks」一键自愈）；可见窗口经 `agents/claude_visible_run.py` 上报完成；`agents/confirm_mcp.py` | ✅ | ✅ 完整回环 |
| agy / Antigravity | `~/.gemini/config/hooks.json` `stackchan` 段 → `agents/antigravity_hook.py` | ✅ CLI 归属 agent=agy | ❌ |
| pi | `~/.pi/agent/extensions/hooks-bridge.ts` → 网关 | ✅ | ❌ |
| vscode | `agents/vscode_hook.py`，任务/终端结束上报 done；`AGENT_CLIS` 已注册；语音派发**已拒发**（防 `code -r` 误开文件） | ✅ | ❌ |

任务执行方式：`agent_query` 打开 agent 自己的可见控制台窗口（标题 `Codex-Asong` /
`ClaudeCode-Asong` / `Antigravity-Asong` / `pi-Asong`，脚本存于 `gateway/state/visible_runs/`），
结果经各 agent hooks 写入网关，机器人唤醒后播报。

> 所有 hook 上报均携带 **msg_uid**（`{agent}_{session8}_{hash(最后一条 assistant 消息)}`），
> 网关按 msg_uid 幂等（重复上报静默 200），固件对 START 回发 ACK，网关收到 ACK 后物理删除
> pending 记录——同一轮任务绝不重复播报，离线消息保留兜底自动重试。

## 机器人固件

- 当前：**v1.2-mqttpush**（`firmware/post-fw-v1.2-mqttpush/`，构建脚本 `build_fw_v112.ps1`）
- 基座：07.31 已跑通的 `reference/stackchan-xiaozhi-firmware`（heavenchenggong 系，
  含「阿松」+ LED 补丁；**不要用 HtSz 主分支**——有 bug 起不来）
- 第二条 MQTT 链路（`stackchan/{mac}/push`）：订阅 EMQX 推送主题，µ-law 直出播放（绕开
  Opus 解码器兼容问题），START 解析 msg_uid → 回发 ACK；SSID 智能路由（EMQX 首选，
  AP 隔离时自动降级）；MQTT buffer 8KB、poll 读超时 5s、**keepalive 15s**；
  lwIP TCP 收窗口 16KB（µ-law 16KB/s 有余量）；播报期间关 WiFi 节能，播完恢复；
  打断后待播放队列排空自然切回待机。
- **Phase 8.1 动作联动**：收到 `done/error` → 点头；`question` → 歪头 +15°；
  待机闲逛摆头 20s 一次。
- **v08.08 LED 根治**：PY32 GPIO13 出厂序列 + `led_manual_` 待机锁 + I2C 互斥，
  真机播报绿→待机暖橙；固件已重建（14:31 产物）。
- **Phase 8.2 拍照**：`robot_snap` → 固件拍 JPEG 分块（`stackchan/{mac}/photo`, QoS1）
  → 网关重组校验。
- v1.0.6/1.0.5/1.0.4 历史版本见下方版本记录。
- 升级：app-only 刷 `xiaozhi.bin @ 0x410000`，保留配置；构建 espressif/idf:v5.5.2
  （5.5.4 会黑屏）。

## 服务与运维

| 服务 | 端口 | 说明 |
|---|---|---|
| 融合网关 | 8010 | 13 个 MCP 工具（含 robot_snap / local_query），Bearer 认证；单 Worker 推送 FIFO + msg_uid 幂等 |
| xiaozhi-mcp 云桥接 | — | mcp_pipe.py + server.py，心跳 60s |
| EMQX 公共 broker | 1883 | 播报推送（broker-cn.emqx.io，QoS0 + 固件 ACK 确认） |
| Codex↔机器人桥接 | — | `bridge/stackchan_mcp.js`（MCP stdio：check_task / respond） |
| xiaozhi-esp32-server (Docker) | — | **已停用**（云链路不依赖） |
| 系统托盘 | — | 状态监视 + 网关守护（单实例保护）+ 队列操作菜单（查看/清空）+「安装/修复 Claude Hooks」自愈菜单 |

守护与计划任务全部经 `wscript.exe` + VBS 隐藏启动（无弹窗），`install_autostart.ps1` 一键注册。

## 故障排查

| 症状 | 处理 |
|---|---|
| 「检查 agent 状态」超时 | 网关/桥接未启动；探测已缓存 120s + 并发（<5s） |
| codex 窗口报 Access denied | `~/.codex/config.toml` `[windows] sandbox='unelevated'`；不要加 `--sandbox workspace-write` |
| 中文任务乱码 | hook 脚本读 UTF-8；`mcp_pipe` 子进程 `PYTHONUTF8=1`（已修复，重启 codex 桌面生效） |
| 机器人念陈旧结果 | `agent_result_check` 只返回 30 分钟内新结果（已修复） |
| 托盘两个图标 | `fusion_tray.ps1` 单实例保护（已修复） |
| 机器人不播报 | 网关日志看 `push ack`/`push no-ack`：ack=已送达；no-ack=机器人 push MQTT 离线，
  消息保留 pending 兜底自动重试；config.json 损坏会 Fail-Fast 拒启 |
| Claude 任务不播报 / hooks 丢失 | 检查 `~/.claude/settings.json` 是否含四钩子（v08.09 起主存
  settings.json：Windows 2.1.x 的 settings.local.json 有 #64699 静默失效 BUG，升级后 hooks
  会彻底不触发）；丢失时右键托盘「安装/修复 Claude Hooks」或重跑
  `agents/install_claude_hooks.ps1` 自愈 |
| 对机器人说「让 vscode 做…」 | 网关返回 "VS Code 暂不支持语音派发任务"——请手动在
  VS Code 运行任务，结束经 `vscode_hook.py` 自动播报（防 `code -r` 误开文件） |
| 播报卡顿/无声 | 确认固件 v1.2-mqttpush（µ-law + 16KB 窗口）；网关日志 `push ok` 后无 ack
  说明机器人 push MQTT 掉线（keepalive 15s 已加固） |

## 已知边界

- 云链路播报为**非打断式主动推送**（EMQX MQTT），消息带 msg_uid + ACK 闭环；
  机器人离线时消息保留 pending，唤醒后 `agent_pending` 补播。
- Codex / Antigravity 桌面应用与 VS Code 插件面板的**内部会话无法外部注入**；
  机器人任务在对应 CLI 窗口执行，插件会话仍经 hooks 上报事件。
- 确认回环仅 claude 完整（`--permission-prompt-tool` + `confirm_mcp`）；
  codex/agy/pi 只上报「需要确认」，回写需在 agent 界面完成。
- 语音端到端延迟约 1.5–2.5s（云端 ASR/LLM/TTS 所致），非打断式播报可接受。
- VS Code 语音派发不支持（已拒发防退化）；拍照依赖机器人联网且 push MQTT 在线。

## 版本记录

### v08.10（2026-08-10）

- **播报规则**：≤50 字完整播报；>50 字本地 LLM 口语化摘要 ≤50 字（失败降级截断），
  先清洗原文不预截断，摘要器拿到完整原文。
- **语速 1.0x**：EdgeTTS `push_tts_rate` +20% → +0%；sherpa 兜底 `tts_fallback_speed`
  1.1 → 1.0。
- **QoS1 投递（吞字根治）**：START / 音频批 / STOP 全部改 QoS1；固件订阅本为 QoS1，
  公网 EMQX 丢包/断连时 broker 重投，消灭 QoS0 静默丢帧导致的播报吞字。
- **Trae 桌面端接入研究**：桌面端原生内置 TraeCode hooks 引擎（无需企业账号），
  全局 hooks 路径 `%USERPROFILE%/.trae-cn/hooks.json`，方案见
  docs/trae-work-integration-feasibility-20260807.md（待实施）。
- 排查记录：云链路 ASR 语种基准确认（xiaozhi.me 云端设置；selfhost language_hints 注释态）。
- **摘要保留完整结论**：提示词强制包含最终结论（根因/结果/决定）；降级改为尾部结论句提取，
  不再头部硬截断丢结论。
- **Hook/Bridge 强壮性**：`scripts/hook_health.py` 自动校验修复 Antigravity/Claude/Codex
  hooks + 链路自检；托盘「Hook 自检与修复」菜单；网关 30 分钟周期自检 + 异常机器人告警
  （agent=system，去重）；托盘已自动拉起 xiaozhi-mcp 云桥接。
- **固件 v1.2-mqttpush 修订**：MQTT 持久会话（disable_clean_session）+ 看门狗 3s→5s；
  重建刷机 0x410000，真机 push ack 验证通过。
- **Hooks 保固规范（务必遵守，防再被改坏）**：Antigravity hooks.json = **扁平结构**
  （条目顶层直接带 `command`，Go 语言服务器只认扁平，嵌套会被 hooks.go:44 拒载）；
  Claude/Codex = **嵌套结构**（各自 loader 认嵌套）。任何"修复"一律交给
  `scripts/hook_health.py`（30 分钟周期自检 + 托盘菜单），严禁手动或让 Agent
  拍平/嵌套化 Antigravity 配置。

### v08.09（2026-08-07 晚）

- **Claude hooks Windows 失效根治**：2.1.224 升级后 settings.local.json hooks 静默失效
  （命中 anthropics/claude-code#64699，重启/回滚均无法恢复）→ hooks 主存迁移到
  ~/.claude/settings.json，真机验证 Stop/SessionEnd 正常触发、机器人 ACK 播报；
  56b9df49 会话完成消息补播成功（17:59 真机 ACK）。
- **install_claude_hooks.ps1 重写**：UTF-8 BOM + PowerShell 5.1/7 双兼容
  （严禁 ConvertFrom-Json -AsHashtable——5.1 会抛错并被 catch 吞掉、清空 env 段）；
  幂等合并写入 settings.json，保留 env/permissions；ccswitch 覆盖后重跑即自愈。
- **托盘新增「安装/修复 Claude Hooks」**：右键一键重跑安装脚本（usion_tray.ps1）。
- **网关两轨修复（吞字根治）**：轨一 _PUSH_BATCH_FRAMES 8→2（每批 ~1.9KB < MTU，
  根治 TCP 分片导致固件丢帧）；轨二 _summarize_for_speech（>60 字经本地 LLM 提炼
  ≤60 字口语化摘要，失败降级截断）+ _prewarm_local_llm 启动预热。
- **config 修正**：local_llm_model qwen3:8b（已删除）→ qwen3.5:9b；push 日志改记实际播报文本。
- 验收：221 字长文本 → 机器人 ACK 播报 50 字 LLM 摘要（18:23:54）；网关 13 工具。

### v08.08（2026-08-07 午后）

- **Antigravity 播报根治**：`~/.gemini/config/hooks.json` 命名空间 `"fusion"`→`"stackchan"`
  （IDE 只识别 stackchan/promlight）；`antigravity_hook.py` 事件别名补
  AfterAgent/agent.stop/SessionEnd/agent.session.end；13:29 真机 push ack。
- **LED 灯环根治**：PY32 GPIO13 未初始化是"写成功但灯不变"的根因，补齐输出+上拉+推挽
  出厂序列；`Py32WriteRegBlock` 失败重试、`refreshLeds` 读-改-写、`led_manual_` 待机锁、
  I2C 互斥 + 触屏故障冷却；固件重建（app-only @0x410000），真机播报绿→待机暖橙 ✅。
- **Prompt v3.6**：LED 灯色归固件，严禁 LLM 调灯表达情绪；用户要求时同轮 `self.led.auto` 恢复。
- 归档 version.08.08 + GitHub 脱敏同步。

### v08.07（2026-08-07）

- **Phase 8.1 动作联动**：固件 `done/error` → 点头 Nod、`question` → 歪头 TiltAsk(+15°)；
  待机摆头 4s → 20s（`kIdleScanIntervalUs` 统一，真机确认）。
- **Phase 8.2 视觉 MCP**：网关 `robot_snap`（12 工具）；固件拍照 JPEG 分块 MQTT
  （`stackchan/{mac}/photo` QoS1）→ 网关重组校验 JPEG 魔数+总长度；连拍 3/3 有效。
- **Claude hooks 抗覆盖**：`install_claude_hooks.ps1` 改写 `~/.claude/settings.local.json`
  （ccswitch 全量覆盖 settings.json 时不再抹掉 hooks），四钩子注入 + 自愈提示。
- **VS Code 拒发**：`agents_core.query()` 对 vscode 显式拒发语音派发任务
  （杜绝 `code -r <task>` 误开文件），手动任务 + `vscode_hook.py` 自动播报。
- **Claude 流式中断兜底**：`claude_hook.py` 摘要为空时强制上报
  "Claude 会话结束(响应可能中断, 详见电脑)"，不再静默丢事件。
- **codex hooks 清理**：`~/.codex/hooks.json` 移除 15 条 PromLight 僵尸钩子
  （备份 `hooks.json.bak-20260807-110621`），仅保留 codex_hook 5 大事件。
- **脱敏加固**：公开副本移除 Tailscale IP `<TAILSCALE_IP>` 与真实本地路径。

### v08.06（2026-08-06 收尾）

- **播报链路根治**：网关 Opus 编码与 ESP 解码器不兼容 → 改传 **µ-law**（16KB/s）；
  lwIP TCP 收窗口 5760B→16KB、MQTT buffer 8KB、poll 读超时 5s、播报期关 WiFi 节能，
  解决"无声/1秒杂音/卡顿"（EMQX RTT ~0.5s 下吞吐有余量）。
- **主动播报闭环**：单 Worker FIFO 串行推流；done/error/question 立即入队主动发声；
  全链路 **msg_uid 幂等**（Hook 生成 uid → 网关去重 → START 带 uid → 固件 ACK →
  网关 ACK-and-Delete 物理删除）；离线消息保留兜底 + 30s 退避自动重试。
- **Agent 耦合补齐**：别名归一化（可头大→codex 等）+ Fail-Fast 存活预检；VS Code 注册
  （AGENT_CLIS + vscode_hook.py）；Claude 交互式 PermissionRequest 钩子（权限弹窗主动播报）；
  Antigravity/Claude/Codex hooks 全部改 msg_uid 去重（废除 120s 窗口）。
- **配置防呆**：config.json 非法即 Fail-Fast 拒启（不再静默回退）；5 个 hook 脚本配置
  解析失败显式 ERROR；claude/antigravity hook 命令改正斜杠（bash 兼容）。
- **托盘/工具**：托盘按云链路口径重写（待推送/事件/确认、最近推送、自建服务器停用）；
  `robot_status` 重写为云链路自检；Codex↔机器人桥接恢复（`bridge/stackchan_mcp.js`）。

### v08.06（2026-08-04）

- 固件 v1.0.6-ttsbuf（已刷入）：TTS 播放缓冲 2.4s→4.8s、播放余量 2→4、入队背压不丢包
- 固件 v1.0.5：麦克风增益 42→36
- **设备端 AEC 回退**：v1.0.3 的 AEC 在 CoreS3 上导致 `audio_input` 线程死循环
  （task_wdt 触发、机器人无反应/重启），addr2line 定位到 dios_ssp AEC DSP，已禁用恢复
  VAD(WebRTC) 管线；唤醒加速与预热连接保留（掉线 2s 秒连）
- Prompt v3 定稿（用户确认）：回复语言跟随 xiaozhi.me 预设；ASR 容错意图兜底
  （按意思推断，禁止把播报/查状态当点歌）；听不清回「再说一遍」
- 云端：STACK 智能体模型 `deepseek-v4-flash-ha` 出现 503 无可用通道 → 换 `qwen3.6`
- **Pending**：长播报时断时续/吞字未根治——v1.0.6 缓冲+背压已上，待排查
  （多段 TTS 段边界 ResetDecoder / 服务器突发推送欠载 / WS 断流）

### v08.05（2026-08-04）

- 固件 v1.0.3-aec-wake（`firmware/post-fw-v1.0.3-aec-wake/`）：
  - 设备端 AEC（ES7210 参考输入消除扬声器回声），聆听模式改 Realtime（可打断、不截尾音）
  - 唤醒加速：multinet 检测窗口 3000→1500ms，阈值下限 0.35→0.30
  - 后台预热连接：待机常驻 WebSocket（15s→120s 指数退避重连），唤醒免重新握手
- Phase 5 决策：P5-1/P5-2（pi/agy 语音确认回环）**舍弃**——pi 走 VS Code、
  agy 走 Antigravity Desktop；P5-4（云端主动推送）**不可行**——xiaozhi.me 无空闲
  自触发 API；P5-5（桌面会话注入）**不可行**——codex app-server daemon 仅 Unix，
  remote-control 是 SSH 配对机制
- 回退备份：Git tag `backup-v08.04` + `backup-v08.04.zip`

### v08.04（2026-08-04）

- 云桥接「开机自启 + keep alive」：StackChan-CloudBridge 登录自启任务（wscript 隐藏）；
  托盘内置桥接守护（进程/心跳异常 30 秒内静默拉起）
- 托盘单实例守卫加固（只认 `-File` 真实实例，避免误杀）
- 桥接启动链路全静默（VBS → powershell Hidden → python Hidden）
- 托盘新增「队列操作」菜单：显示队列消息内容 / 清空队列（自动备份）/ 清空待确认

### v08.03（2026-08-03）

- 云链路 + 唤醒播报（prompt v2、agent_pending 唤醒优先规则）
- 固件 v1.0.2-micfix（麦克风增益 42，识别修复）
- 四 agent hooks 全部打通（codex/claude/agy/pi），可见窗口执行
- claude 可见窗口完成事件（`agents/claude_visible_run.py`：结果同时进事件队列与
  outbox，唤醒播报 + 主动问结果两条路都通）
- 修复：codex Access denied、agent_status 超时（13.9s→4.8s）、中文乱码、陈旧结果、
  claude 无完成事件、claude/pi 工作目录、托盘双图标、计划任务弹窗
- 归档：`version.08.03/`（含当日全量包）

历史版本：`firmware/post-fw-v1.0.0-led`（07.31 跑通版，可回退）。

## 参考项目与致谢

本方案参考/使用了以下开源项目，感谢各位作者：

| 项目 | 作者 | 用途 |
|---|---|---|
| [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | @xinnan-tech | 自建 xiaozhi 服务器（备用链路） |
| [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) | @78 | 设备端固件上游 |
| [stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge) | @heavenchenggong | StackChan × Claude 桥接固件（07.31 跑通基座来源） |
| [StackChan-HtSz](https://github.com/mo-hantang/StackChan-HtSz) | @mo-hantang | StackChan-HtSz 固件（主分支） |
| [StackChan](https://github.com/hylarucoder/StackChan) | @hylarucoder | StackChan 参考实现（舵机/动作/LED） |
| [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) | @migratorywhale | StackChan × MCP 参考 |
| [pi-coding-agent](https://github.com/earendil-works/pi-coding-agent) | @earendil-works | pi 编程智能体 |
| [mcp-calculator](https://github.com/78/mcp-calculator) | @78 | MCP 工具编写示例 |

以及各 AI agent 官方产品：OpenAI Codex、Anthropic Claude Code、Google Antigravity（Gemini CLI）。

## 敏感信息

本仓库**不含任何真实凭据**：token / API key / MAC / 域名均为占位符
（`YOUR_*` / `AA:BB:CC:DD:EE:FF`）。真实值只存在于本机 `.env`、`config.json`、
docker 配置。`.gitignore` 已忽略所有运行时敏感文件。
部署时按 [DEPLOY.md](DEPLOY.md) 第 4 节逐项替换。

