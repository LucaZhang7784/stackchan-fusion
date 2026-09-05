# Docker MCP Toolkit 接入说明（2026-08-01）

## 目标

用 Docker Desktop 内置的 MCP Toolkit（`docker mcp` CLI）作为统一 MCP 网关，
把本机 fusion-gateway（localhost:8010，11 个工具）暴露给 Codex / Claude Code / VS Code。

## 已完成的配置

1. Profile `stackchan` 已创建，包含 1 个远程服务器：
   - name: `fusion-gateway`
   - endpoint: `http://localhost:8010/mcp`（streamable-http + Bearer 认证）
   - 源定义文件：`C:\Users\zhang.luca\.docker\mcp\catalogs\fusion-gateway.yaml`
2. 已导出可移植配置：`docker/mcp-toolkit-profile.json`
3. 已连接的客户端（全局/项目）：
   - Codex（全局）：`.codex/config.toml` → `[mcp_servers.MCP_DOCKER]`
   - Claude Code（全局）：`~/.claude.json` → `MCP_DOCKER`
   - VS Code（项目）：`${STACKCHAN_ROOT}\.vscode\mcp.json`
4. 环境变量已持久化（用户级）：
   - `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1`
   - 作用：允许 gateway 通过 http 连接本地 fusion-gateway（Toolkit 默认强制 https）

## 连接结构

```
Codex Desktop / Claude Code CLI / VS Code MCP
        │  stdio: docker mcp gateway run --profile stackchan
        ▼
Docker MCP Gateway (Toolkit)
        │  streamable-http: http://localhost:8010/mcp  + Bearer
        ▼
fusion-gateway.py (Windows 进程, PID 动态, :8010)
        │  agent_query / agent_status / agent_pending / agent_confirm ...
        ▼
agents_core.py → agy / pi / claude / codex CLI
```

## 常用命令

```powershell
# 查看 profile
docker mcp profile show stackchan

# 查看已连接的客户端
docker mcp client ls --global

# 列出当前 gateway 可见的工具（dry-run）
$env:DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS='1'
docker mcp gateway run --profile stackchan --dry-run

# 导入 profile（换机时）
docker mcp profile import .\mcp-toolkit-profile.json

# 重新连接客户端
docker mcp client connect codex --global --profile stackchan
docker mcp client connect claude-code --global --profile stackchan
docker mcp client connect vscode --profile stackchan   # 在项目根执行
```

## 验证结果（2026-08-01 已实测）

- `docker mcp gateway run --profile stackchan` 列出 19 个工具：
  agent_status / agent_query / agent_pending / agent_confirm / claude_query /
  codex_query / docker_status / robot_say / robot_pending / robot_status / ws_probe
  + 8 个 Toolkit 动态管理工具（mcp-find / mcp-add / mcp-exec ...）
- `agent_status(all)`：claude 2.1.220 / codex 0.146.0 / agy 1.1.9 / pi 0.80.3 全部可用
- `agent_query(pi, "计算 1+1")` → 返回 `2`

## 注意事项

- 首次连接后客户端需要重启（Codex / Claude Code / VS Code）才能加载 MCP_DOCKER。
- fusion-gateway 必须保持运行（`gateway\run_gateway.ps1` 或计划任务）。
- `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS` 是 Toolkit 的安全开关，仅本机调试需要；
  若 gateway 未来改成 https 可移除。
- profile 中存有 Bearer token（`config.json` 的 `auth_token`），换机导入后需同步。

## 追加：PromLight MCP（2026-08-03）

`stackchan` profile 现在含 2 个服务器：`fusion-gateway`（:8010）+ `promlight`（:8011）。
所有走 `MCP_DOCKER` 的客户端（Codex / Claude Code / VS Code / Gemini）自动获得 7 个
PromLight 工具：scan / connect / disconnect / get_battery / get_services /
read_characteristic / send_keys。

### 架构（与 fusion-gateway 同模式）

```
Codex Desktop / Claude Code CLI / VS Code MCP
        │  stdio: docker mcp gateway run --profile stackchan
        ▼
Docker MCP Gateway (Toolkit)
        │  streamable-http: http://localhost:8011/mcp + Bearer
        ▼
promlight server.py --transport http  (Windows 进程, 127.0.0.1:8011)
        │
        ▼ bleak → PromLight BLE 设备
```

### 涉及文件

- MCP server（stdio 默认 / `--transport http` 可选）：`C:\Users\zhang.luca\.opencode\mcp-servers\promlight\server.py`
- catalog 定义：`C:\Users\zhang.luca\.docker\mcp\catalogs\promlight.yaml`
- 自启动：计划任务 `PromLight-MCP`（登录时）+ `PromLight-MCPWatchdog`（每 5 分钟拉起），脚本
  `C:\Users\zhang.luca\.opencode\mcp-servers\promlight\run_promlight_mcp.ps1`
- 灯态映射：`D:\PromLight\events.json`（work=黄灯常亮 / await·error=红灯闪烁 / idle·end=绿灯常亮）

### 验证（2026-08-03 已实测）

- `docker mcp gateway run --profile stackchan --dry-run` → promlight (7 tools) + fusion-gateway (11 tools)
- 8011 `/healthz` 免认证；`/mcp` 无 Bearer 返回 401，带 Bearer 正常握手
- 灯命令实测：`led yellow on --only` / `led red blink --only` / `led green on --only`
  均 `ok:true` 下发到设备 F07E
