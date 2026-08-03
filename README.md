# StackChan 融合方案

让 **StackChan 桌面机器人**（M5Stack CoreS3）通过语音指挥本机的
**codex / claude / agy / pi** 四类 AI agent：查询状态、派发任务、播报结果、语音确认。

> 核心思路（2026-08-03）：**云链路 + 唤醒播报**。机器人走 xiaozhi.me 云端智能体，
> 每次唤醒自动检查待播报消息并逐条念出；机器人派发的任务在 **agent 自己的可见窗口**
> 执行，结果经 hooks 回流到机器人。自建 xiaozhi-esp32-server 链路保留为备用。

## 架构

```
机器人 (M5Stack CoreS3, 固件 v1.0.2-micfix, 唤醒词「阿松」)
   │ 语音 (ASR/LLM/TTS 在 xiaozhi.me 云端)
   ▼
xiaozhi.me 云智能体 (STACK, 提示词见 prompt-阿松-v2.md)
   │ MCP (wss://api.xiaozhi.me/mcp)
   ▼
xiaozhi-mcp 云桥接 (mcp_pipe.py + server.py, 本机)
   │ agent_status / agent_query / agent_pending / agent_confirm / agent_result_check ...
   ▼
融合网关 fusion_gateway.py (:8010, Bearer 认证)
   │
   ├── codex   (hooks: 任务开始/完成/需审批 → 机器人)
   ├── claude  (hooks + confirm_mcp 确认回环)
   ├── agy     (Antigravity fusion hooks, CLI 归属 agent=agy)
   └── pi      (扩展 hooks-bridge.ts)
        └── 机器人任务 → agent 自己的可见窗口执行 (Codex-Asong / ClaudeCode-Asong / ...)
```

两条链路：

| 链路 | 说明 |
|---|---|
| 云链路（主） | 机器人语音走 xiaozhi.me；agent 事件经网关排队，唤醒后播报 |
| 自建链路（备用） | 本机 docker xiaozhi-esp32-server + Tailscale Funnel；支持 `robot_say` 真推送 |

## 功能

| 能力 | 说明 |
|---|---|
| 唤醒播报 | 每次唤醒第一动作查 `agent_pending`，有消息逐条念、念完清除 |
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
| claude | `~/.claude/settings.json` hooks → `agents/claude_hook.py`；`agents/confirm_mcp.py` | ✅ | ✅ 完整回环 |
| agy / Antigravity | `~/.gemini/config/hooks.json` `fusion` 段 → `agents/antigravity_hook.py` | ✅ CLI 归属 agent=agy | ❌ |
| pi | `~/.pi/agent/extensions/hooks-bridge.ts` → 网关 | ✅ | ❌ |

任务执行方式：`agent_query` 打开 agent 自己的可见控制台窗口（标题 `Codex-Asong` /
`ClaudeCode-Asong` / `Antigravity-Asong` / `pi-Asong`，脚本存于 `gateway/state/visible_runs/`），
结果经各 agent hooks 写入网关，机器人唤醒后播报。

## 机器人固件

- 当前：**v1.0.2-micfix**（`firmware/post-fw-v1.0.2-micfix/`）
- 基座：07.31 已跑通的 `reference/stackchan-xiaozhi-firmware`（heavenchenggong 系，
  含「阿松」+ LED 补丁；**不要用 HtSz 主分支**——有 bug 起不来）
- 改动：麦克风输入增益 30→42（修复语音识别差）；唤醒词「阿松」；分区 post-fw
  （app @ 0x410000，16MB）
- 升级：app-only 刷 `xiaozhi.bin @ 0x410000`，保留配置（`firmware/post-fw-v1.0.2-micfix/flash_post_fw.ps1`）
- 构建：espressif/idf:v5.5.2（5.5.4 会黑屏），流程见 `firmware/build_led_ci.sh`

## 服务与运维

| 服务 | 端口 | 说明 |
|---|---|---|
| 融合网关 | 8010 | 11 个 MCP 工具，Bearer 认证 |
| xiaozhi-mcp 云桥接 | — | mcp_pipe.py + server.py，心跳 60s |
| xiaozhi-esp32-server (Docker) | 8000/8003 | 备用链路 |
| mcp-endpoint-server (Docker) | 8004 | 备用链路 MCP 端点 |
| funnel_proxy.py | 8090 | 备用路由（开机自启 + 5 分钟自愈） |
| 系统托盘 | — | 状态监视 + 网关守护（单实例保护） |

守护与计划任务全部经 `wscript.exe` + VBS 隐藏启动（无弹窗），`install_autostart.ps1` 一键注册。

## 故障排查

| 症状 | 处理 |
|---|---|
| 「检查 agent 状态」超时 | 网关/桥接未启动；探测已缓存 120s + 并发（<5s） |
| codex 窗口报 Access denied | `~/.codex/config.toml` `[windows] sandbox='unelevated'`；不要加 `--sandbox workspace-write` |
| 中文任务乱码 | hook 脚本读 UTF-8；`mcp_pipe` 子进程 `PYTHONUTF8=1`（已修复，重启 codex 桌面生效） |
| 机器人念陈旧结果 | `agent_result_check` 只返回 30 分钟内新结果（已修复） |
| 托盘两个图标 | `fusion_tray.ps1` 单实例保护（已修复） |
| 机器人不播报 | 确认已唤醒 + 云智能体 prompt 是 v2（`prompt-阿松-v2.md`） |

## 已知边界

- 云链路**无打断式推送**：agent 事件排队，机器人唤醒后经 `agent_pending` 播报；
  自建链路才支持 `robot_say` 真推送。
- Codex / Antigravity 桌面应用与 VS Code 插件面板的**内部会话无法外部注入**；
  机器人任务在对应 CLI 窗口执行，插件会话仍经 hooks 上报事件。
- 确认回环仅 claude 完整（`--permission-prompt-tool` + `confirm_mcp`）；
  codex/agy/pi 只上报「需要确认」，回写需在 agent 界面完成。
- 语音端到端延迟约 1.5–2.5s（云端 ASR/LLM/TTS 所致），非打断式播报可接受。

## 版本记录

### v08.03（2026-08-03）

- 云链路 + 唤醒播报（prompt v2、agent_pending 唤醒优先规则）
- 固件 v1.0.2-micfix（麦克风增益 42，识别修复）
- 四 agent hooks 全部打通（codex/claude/agy/pi），可见窗口执行
- 修复：codex Access denied、agent_status 超时（13.9s→4.8s）、中文乱码、陈旧结果、
  托盘双图标、计划任务弹窗
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
