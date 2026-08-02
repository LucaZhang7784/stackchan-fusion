# StackChan 融合方案 — fusion.firmware.0731

日期: 2026-08-01
范围: 把「xiaozhi.me 云智能体 + Tailscale」与「stackchan 机器人工具能力」融合,
打通 **机器人 <-> agy / pi / claude / codex 四 agent 双向通讯**。

> 当前主链路（2026-08-01 已实测）: 机器人走 xiaozhi.me 云 STACK 智能体,
> 经 xiaozhi-mcp bridge 连接本机 fusion-gateway, 再由 **Docker MCP Toolkit**
> 统一暴露给 Codex / Claude Code / VS Code。自建 xiaozhi-esp32-server 链路保留备用。

## 一、结论(先看这里)

1. **机器人固件**: M5Stack 官方固件（已解绑激活码），xiaozhi.me 云绑定 STACK 智能体。
2. **机器人 -> agent**: 云 LLM 经 xiaozhi-mcp bridge（wss 接入点）拿到本地 8 个工具
   （agent_status / agent_query / agent_pending / agent_confirm / claude_query /
   codex_query / docker_status / agent_result_check），语音指令直接驱动本地 agent。
3. **agent -> 机器人**: agent 事件经网关排队，机器人唤醒后由 LLM 调 agent_pending 播报
   （云链路无推送通道；自建 xiaozhi-esp32-server 链路可用 robot_say 主动推送）。
4. **电脑端接入**: Docker MCP Toolkit（profile=stackchan）统一暴露 11 个网关工具给
   Codex / Claude Code / VS Code，客户端零配置直接可用。
5. **连通性验证**: 网关 /healthz + `docker mcp gateway run --dry-run` +
   机器人语音实测（例: 问「codex 状态」→ 机器人播报「codex 状态正常」）。

## 二、架构

```
                 ┌──────────────────────── 本机 (Windows) ────────────────────────┐
 机器人(ESP32)   │  Docker: xiaozhi-esp32-server (8000/8003)                      │
 官方固件2.2.6   │    ├─ SERVER_MCP ──► fusion_gateway.py (8010, Bearer 认证)      │
   │ 出站 WSS    │    └─ MCP接入点 ──► mcp-endpoint-server (8004, 旧bridge仍挂着)  │
   ▼            │                                 ▲                               │
 Funnel 443     │        ┌────────────────────────┴───────────────┐               │
 (Tailscale)    │  Codex / Claude Code  (MCP client -> 8010)       │               │
                │    robot_say / robot_status / codex_query ...    │               │
                └──────────────────────────────────────────────────┘               │
```

- 网关两种用法: `--http` 给 xiaozhi SERVER_MCP 与 Claude Code(http MCP); `--stdio` 给 Codex CLI/其他 stdio 客户端。
- HTTP 模式强制 `Authorization: Bearer <token>`(fail-closed), 只有 `/healthz` 免认证。

## 三、文件清单

| 路径 | 说明 |
|---|---|
| gateway/fusion_gateway.py | 融合网关主程序(单文件, 无框架依赖) |
| gateway/config.json | 实际配置(OTA/MAC/health key/token/端口) |
| gateway/agents_core.py | 多 agent 管理核心(agy/pi/claude/codex) + 事件/确认存储 |
| gateway/run_gateway.ps1 / stop_gateway.ps1 | 网关启停 |
| gateway/watchdog_gateway.ps1 | 网关守护(每 2 分钟检查, 挂了自动拉起) |
| gateway/fusion_tray.ps1 | 系统托盘状态工具(网关/MCP/机器人三色状态) |
| gateway/install_autostart.ps1 | 一键注册: 网关自启 + watchdog + 托盘 |
| docker/fusion-gateway.yaml | MCP Toolkit server 定义(remote + streamable-http + Bearer) |
| docker/mcp-toolkit-profile.json | profile `stackchan` 导出(迁移用) |
| docker/host-executor.py | Windows 宿主执行器(容器内 gateway 调本地 CLI 用) |
| docker/run_executor.ps1 / install_executor_task.ps1 | 执行器启动 + 自启 |
| docker/MCP-Toolkit接入说明.md | Toolkit 接入/验证完整文档 |
| gateway/守护与托盘说明.md | 守护与托盘使用说明 |
| server/.mcp_server_settings.json | 给 xiaozhi 的 SERVER_MCP 新配置(streamable-http) |
| server/deploy_server_mcp.ps1 | SERVER_MCP 配置部署(备份->替换->重启->验证/回滚) |
| server/deploy_fusion_push.ps1 | 一键部署: 推送补丁挂载 + fusion_secret + SERVER_MCP + 容器重建 |
| server-patch/core/*.py | 服务器补丁(connection 注册表 / http_server /api/push) |
| server-patch/docker-compose.fusion.yml | 补丁覆盖挂载(与主 compose 一起用) |
| server/prompt_patch.md | 提示词补丁: 唤醒后检查待播报消息 |
| scripts/verify_connectivity.py | 分层连通性验证 |
| scripts/stop_legacy_bridge.ps1 | 停止已废弃的旧 bridge.js |
| tests/test_gateway.py | 网关自检(stdio JSON-RPC) |
| firmware/remote_wakeup_v2.md | v2 主动播报路线分析(A/B/C) |
| package-stackchan.zip | 全量迁移包(固件 + PC 端全套 + README) |

## 四、部署步骤

```powershell
# 1. 启动网关
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\gateway\run_gateway.ps1

# 2. 网关自检
python <PROJECT_DIR>\tests\test_gateway.py

# 3. 部署到 xiaozhi server (SERVER_MCP 注册 + 推送补丁 /api/push, 自动停/启容器, 带备份与回滚)
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\server\deploy_server_mcp.ps1

# 4. (可选)提示词补丁, 让机器人唤醒后主动取消息
#    按 server/prompt_patch.md 操作后重启容器

# 5. 连通性验证
python <PROJECT_DIR>\scripts\verify_connectivity.py
```

## 五、Agent 侧接入方式

**推荐方式（当前在用）**: Docker MCP Toolkit 统一接入, 见第九章。
客户端配置由 `docker mcp client connect` 自动写入:

- Codex: `~/.codex/config.toml` → `[mcp_servers.MCP_DOCKER]`
- Claude Code: `~/.claude.json` → `MCP_DOCKER`
- VS Code: `<项目根>/.vscode/mcp.json` → `MCP_DOCKER`

**直连方式（备选）**:

- Claude Code:
  ```
  claude mcp add --transport http fusion http://127.0.0.1:8010/mcp
  ```
  (若 CLI 需要头: 配置 headers Authorization: Bearer YOUR_GATEWAY_TOKEN)
- Codex CLI (~/.codex/config.toml):
  ```toml
  [mcp_servers.fusion]
  command = "python"
  args = ["<PROJECT_DIR>/gateway/fusion_gateway.py", "--transport", "stdio"]
  ```
- 注意: Codex 已换回 CLI 版 (0.146.0, 可后台启动), 商店版 Access denied 问题已解决。

## 六、故障排查

| 症状 | 检查 |
|---|---|
| 服务器日志 `服务端MCP客户端已连接，可用工具: []` | 网关没起/容器连不上 8010/token 不匹配 |
| `unhandled errors in a TaskGroup` | 旧 `type:"ws"` 配置仍在(本方案已替换); 或网关地址不可达 |
| 机器人听不到任何播报 | 先跑 verify_connectivity.py, 再人工唤醒对话 |
| 想停旧 bridge | scripts/stop_legacy_bridge.ps1 -Kill (guard 可能拉起, 需同时停 guard) |
| Tailscale 重连后网关不可达 | 容器内连的是 YOUR_TAILSCALE_IP:8010, 确保 Tailscale IP 未变 |

## 六.5、部署状态 (2026-08-01 实测)

| 环节 | 状态 |
|---|---|
| xiaozhi.me 云 STACK 智能体 | ✅ 设备 ID YOUR_DEVICE_ID 已绑定, 智能体 STACK |
| xiaozhi-mcp bridge (wss) | ✅ 运行中, 8 工具, 心跳正常(每 60s Ping) |
| 融合网关 (8010, Bearer 认证) | ✅ 运行中, /healthz 200, 11 工具 |
| Docker MCP Toolkit | ✅ profile stackchan 完整加载, 19 工具可见 |
| Codex / Claude Code / VS Code 客户端 | ✅ 全部 connected (MCP_DOCKER) |
| agent 探测 | ✅ claude 2.1.220 / codex 0.146.0 / agy 1.1.9 / pi 0.80.3 |
| 端到端工具调用 | ✅ agent_query(pi, "1+1") → 2 |
| 机器人语音实测 | ✅ 「查 codex 状态」→ 机器人播报「codex 状态正常」 |
| 网关守护 + 托盘 | ✅ watchdog 实测 kill 后 6s 自动拉起; 托盘三色状态正常 |

部署中修掉的三个坑:
1. .ps1 中文乱码 → 所有脚本改存 UTF-8 BOM。
2. .config.yaml 曾被 ANSI 读取写坏 → 已从备份恢复, 部署脚本改用 .NET UTF-8 读写。
3. FastMCP 1.28 两个坑: 外层包装必须传播内层 lifespan; 传输安全默认拒绝非 localhost 的 Host 头(421) → 已加 allowed_hosts。

另修: MCP Toolkit profile 缺 `description` 字段导致 UI "Failed to load profiles" →
补齐 description/icon/readme/metadata 后正常加载。

## 七、已知边界

- v1 的 agent->机器人是「队列+唤醒播报」, 不是打断式推送; 真·主动播报见 firmware/remote_wakeup_v2.md。
- MQTT 远程唤醒(官方路径)在本网络(AP隔离+Funnel 不支持 UDP/1883)下不可行, 除非 MQTT 上公网。
- M5Stack 官方固件原生已有 8 个设备工具(音量/屏幕/LED/拍照 self.camera.take_photo 等), 已并入函数列表。

---

## 八、多 Agent 双向通话 (v2, 2026-08-01)

机器人 ↔ 本机 4 个 agent 及其 VS Code 插件双向通讯:
**agy (Antigravity CLI) / pi (pi-coding-agent) / claude (Claude Code CLI) / codex (Codex CLI)**。

### 架构

```
机器人(云端 STACK 智能体 / 自建 docker 两用)
   │ 语音
   ▼
xiaozhi LLM ──工具调用──► xiaozhi-mcp bridge (云) 或 融合网关 (自建)
                              │ agents_core.py (共享事件/确认存储)
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
   agent_query            agent_pending         agent_confirm
   (agy/pi/claude/codex   (待播报事件+待确认问题)  (语音回答回写)
    无头执行, 记 done 事件)
                              ▲
   agent 侧事件 ──► 融合网关 POST /api/agent_event (hooks/包装器上报)
   claude 权限确认 ──► agents/confirm_mcp.py (permission-prompt-tool)
```

### 工具清单 (云 bridge 8 个 / 网关 11 个)

| 工具 | 说明 |
|---|---|
| agent_status(agent=all) | 4 个 agent 的 CLI 可用性/运行进程/待确认数/最近事件 |
| agent_query(agent, task) | 无头执行 agy/pi/claude/codex, 结果进事件+outbox |
| agent_pending(clear) | 机器人端读待播报事件与待确认问题 |
| agent_confirm(agent, answer) | 把用户语音回答回写给等待中的 agent |
| claude_query / codex_query / docker_status / robot_say / robot_pending | v1 工具保留 |

### 确认回环 (claude 权限请求 → 机器人 → 语音回答 → claude)

- `agents/confirm_mcp.py`: MCP 服务端, 作为 claude `--permission-prompt-tool`
- `agents/claude_run.py`: 包装器, 自动带 confirm MCP 运行 `claude -p`
- `agents/claude_hook.py` + `install_claude_hooks.ps1`: 给 ~/.claude/settings.json
  装 Stop/SessionEnd/Notification hooks, VS Code 里的 claude 会话也会上报
- 流程: claude 要权限 → confirm_mcp 注册问题 → 网关排队(自建可推送) →
  机器人唤醒后 LLM 读 agent_pending 念出 → 用户回答 → LLM 调 agent_confirm →
  回答写回 reply_file → confirm_mcp 返回 allow/deny → claude 继续

### 用法

```powershell
# claude 带确认回环
python <PROJECT_DIR>\agents\claude_run.py "任务描述" "工作目录"
# 给 VS Code/终端 claude 会话装 hooks
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\agents\install_claude_hooks.ps1
```

### 云端 STACK 智能体角色介绍(已贴入 xiaozhi.me 控制台, 唤醒词「阿松」)

```
我叫阿松, 桌面陪伴 AI, 活泼可爱, 回复 1-2 句不超过 50 字。
工具规则:
- 用户问电脑上 agent 状态/谁能用: 调 agent_status
- 用户让 agy/pi/claude/codex 做事: 调 agent_query, 立刻回复"正在执行"
- 用户问「有没有消息/待办/谁找我」: 先调 agent_pending 念出来
- 用户回答 agent 的待确认问题: 调 agent_confirm(agent, 回答)
- 用户问「结果出来了吗」: 调 agent_result_check 或 agent_pending
- 简单问答/闲聊/控制设备(点头/灯/拍照)直接回答或用设备工具
【LED 灯环反馈规则】
- 用户在说话/你在聆听时: 调 self.led.set_color, r=0, g=120, b=255(蓝色)
- 开始播报回复前: 调 self.led.set_color, r=0, g=255, b=90(绿色)
- 播报结束后: 调 self.led.auto(待机暖橙)
- 用户要求具体颜色时: 按用户说的颜色调
- 重要: 每个阶段只调用一次; LED 工具失败时忽略
```

> 注: 当前固件为基于 esp32 的修改版(唤醒词「阿松」+ LED 状态灯),
> 烧录文件见 `firmware/` (merged-binary.bin + xiaozhi.bin)。

### 已知边界

- 云链路无推送通道: agent 事件在网关排队, 机器人**唤醒后**由 LLM 读
  agent_pending 播报(非打断式); 自建链路可用 robot_say 推送。
- 确认回环完整支持 claude (permission-prompt-tool); agy/pi 无头查询可用,
  交互式确认待其 CLI 支持; codex CLI 查询可用, 确认机制待官方支持。
- pi 必须 `--no-context-files` 且 workdir=用户主目录(某些目录下会报
  "content is not iterable")。
- VS Code 插件: claude 扩展经 hooks 上报; pi 扩展无 hooks 暂未接入。

---

## 九、Docker MCP Toolkit 统一接入 (2026-08-01)

使用 Docker Desktop 内置的 **MCP Toolkit**（`docker mcp` CLI）作为统一 MCP 网关，
把本机 fusion-gateway（localhost:8010，11 个工具）暴露给
**Codex / Claude Code / VS Code**，客户端只需连接一个 `MCP_DOCKER` 入口。

### 9.1 结构

```
Codex Desktop / Claude Code CLI / VS Code MCP
        │  stdio: docker mcp gateway run --profile stackchan
        ▼
Docker MCP Gateway (Toolkit, profile=stackchan)
        │  streamable-http: http://localhost:8010/mcp + Bearer
        ▼
fusion_gateway.py (Windows, :8010, 11 工具)
        │  agent_query / agent_status / agent_pending / agent_confirm ...
        ▼
agents_core.py → agy / pi / claude / codex CLI
```

### 9.2 关键文件

| 路径 | 说明 |
|---|---|
| pc/docker/fusion-gateway.yaml | MCP Toolkit server 定义(remote + streamable-http + Bearer) |
| pc/docker/mcp-toolkit-profile.json | profile `stackchan` 导出(迁移用) |
| pc/docker/MCP-Toolkit接入说明.md | 完整接入/验证步骤 |
| pc/gateway/watchdog_gateway.ps1 | 网关守护(每 2 分钟检查, 挂了自动拉起) |
| pc/gateway/fusion_tray.ps1 | 系统托盘状态工具(网关/MCP/机器人三色状态) |
| pc/gateway/守护与托盘说明.md | 守护与托盘使用说明 |

### 9.3 环境要求

- Docker Desktop 4.62+（实测 4.84.0）
- 环境变量（用户级）: `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1`
  （允许 Toolkit 经 http 连接本地 fusion-gateway；Toolkit 默认强制 https）
- fusion-gateway 必须运行: `pc/gateway/run_gateway.ps1`

### 9.4 迁移到新电脑

```powershell
# 1) 安装依赖
pip install mcp uvicorn starlette websockets python-dotenv

# 2) 拷贝 pc/docker/fusion-gateway.yaml 到新电脑
Copy-Item .\pc\docker\fusion-gateway.yaml $HOME\.docker\mcp\catalogs\

# 3) 导入 profile（含 endpoint/Bearer 配置）
docker mcp profile import .\pc\docker\mcp-toolkit-profile.json

# 4) 设置环境变量并连接客户端
[Environment]::SetEnvironmentVariable('DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS','1','User')
docker mcp client connect codex --global --profile stackchan
docker mcp client connect claude-code --global --profile stackchan
# (VS Code 在项目根) docker mcp client connect vscode --profile stackchan

# 5) 启动网关 + 守护 + 托盘
powershell -NoProfile -ExecutionPolicy Bypass -File .\pc\gateway\install_autostart.ps1
```

> 注意: profile 中带有 Bearer token（对应 gateway/config.json 的 auth_token），
> 换机后两处需保持一致。新电脑的 xiaozhi-mcp/.env 需填自己的 MCP_ENDPOINT。

### 9.5 守护与托盘（本机已启用）

两个计划任务（install_autostart.ps1 一键注册，均带 `-WindowStyle Hidden` 静默运行）:

| 任务 | 触发 | 内容 |
|---|---|---|
| StackChan-FusionGateway | 登录时 | 启动网关（静默） |
| StackChan-FusionTray | 登录时 | 系统托盘状态工具 |

托盘每 5 秒轮询: 网关 /healthz、MCP profile、机器人 bridge 心跳,
图标绿色=全正常 / 橙色=部分异常 / 红色=网关离线, 状态变化弹气泡提醒,
右键可查看详情、重启网关、退出托盘。详见 gateway/守护与托盘说明.md。

**守护逻辑内置于托盘**: 检测到网关离线会自动静默拉起（30 秒防抖），
不依赖任何定时计划任务，因此不会定时弹 PowerShell 窗口。
详见 gateway/守护与托盘说明.md。

### 9.6 备份与打包

`package_stackchan.py` 一键生成 `package-stackchan/` + `package-stackchan.zip`
（含固件 7 个 bin、PC 端 gateway/xiaozhi-mcp/agents/docker 全套、README），
密钥自动替换为占位符。迁移用 zip 即可, 见 9.4。

---

## 十、敏感信息替换说明 (发布前必读)

本仓库为**脱敏版本**: 所有 token / 密钥 / 激活码 / 设备标识 / 内网地址
均已替换为占位符。克隆后在部署前, 请按下表把你的真实值填回去。
**不要**把真实密钥提交到 GitHub。

| 占位符 | 含义 | 出现位置 |
|---|---|---|
| `YOUR_GATEWAY_TOKEN` | 融合网关 Bearer 认证 token | gateway/config.json.example, docker/fusion-gateway.yaml, docker/mcp-toolkit-profile.json |
| `YOUR_HEALTH_KEY` | xiaozhi MCP 接入点 health key | gateway/config.json.example |
| `YOUR_FUNNEL_DOMAIN.ts.net` | Tailscale Funnel 域名 | gateway/config.json.example |
| `YOUR_TAILSCALE_IP` | Tailscale 内网 IP | server/.mcp_server_settings.json |
| `AA:BB:CC:DD:EE:FF` | 机器人 MAC 地址 | gateway/config.json.example |
| `YOUR_DEVICE_ID` | xiaozhi.me 设备 ID | README 部署状态表 |
| `YOUR_TOKEN_HERE` | xiaozhi.me MCP 接入点 JWT | xiaozhi-mcp/.env.example |
| `<PROJECT_DIR>` | 本机项目绝对路径 | 各 .ps1 / mcp_config.json |
| `<USER_HOME>` | 本机用户目录 | 部分脚本 |

### 替换步骤

1. 把 `gateway/config.json.example` 复制为 `gateway/config.json`, 填入:
   `ota_url`(你的 Funnel 域名)、`robot_mac`、`endpoint_health_url` 的 key、
   `auth_token`(自定义一个强随机串)。
2. 把 `xiaozhi-mcp/.env.example` 复制为 `xiaozhi-mcp/.env`, 填入
   xiaozhi.me 控制台的 MCP 接入点(含 JWT)。
3. 保持 `docker/fusion-gateway.yaml` 与 `docker/mcp-toolkit-profile.json`
   里的 Bearer 与 config.json 的 `auth_token` 一致。
4. 按第 9.4 节执行 MCP Toolkit 迁移。

### 发布检查清单

- [ ] 全局搜索 `YOUR_` 占位符均已替换为真实值
- [ ] 不要把 `.env` / `config.json` / `*.log` 加入 git(见 .gitignore)
- [ ] 推送前用 `git diff --cached` 复查没有意外提交密钥