# StackChan Fusion — fusion.firmware.0731

> **中文版**: [README.md](README.md) · **日本語版**: [README.ja.md](README.ja.md)

Date: 2026-08-01
Scope: Integrate "xiaozhi.me cloud agent + Tailscale" with "StackChan robot tool
capabilities" to enable **bidirectional voice communication between the robot and
four local agents: agy / pi / claude / codex**.

> Current primary link (verified 2026-08-01): the robot uses the xiaozhi.me cloud
> STACK agent, connects to the local fusion-gateway through the xiaozhi-mcp bridge,
> and is exposed to **Codex / Claude Code / VS Code** via **Docker MCP Toolkit**.
> The self-hosted xiaozhi-esp32-server link is kept as a fallback.

## 1. Conclusions (read this first)

1. **Robot firmware**: M5Stack official firmware (activation code unbound), bound
   to the STACK agent on xiaozhi.me.
2. **Robot → Agent**: the cloud LLM obtains 8 local tools through the xiaozhi-mcp
   bridge (wss endpoint) — `agent_status` / `agent_query` / `agent_pending` /
   `agent_confirm` / `claude_query` / `codex_query` / `docker_status` /
   `agent_result_check` — so voice commands drive local agents directly.
3. **Agent → Robot**: agent events are queued in the gateway; after wake-up the
   robot's LLM calls `agent_pending` to read them aloud (the cloud link has no
   push channel; the self-hosted xiaozhi-esp32-server link can proactively push
   with `robot_say`).
4. **Desktop integration**: Docker MCP Toolkit (profile=stackchan) exposes all 11
   gateway tools to Codex / Claude Code / VS Code with zero client config.
5. **Connectivity verification**: gateway `/healthz` + `docker mcp gateway run
   --dry-run` + real robot voice test (e.g. ask "codex status" → robot replies
   "codex status OK").

## 2. Architecture

```
                 ┌────────────────────── Local (Windows) ──────────────────────┐
 Robot (ESP32)   │  Docker: xiaozhi-esp32-server (8000/8003)                   │
 Official FW 2.2.6│    ├─ SERVER_MCP ──► fusion_gateway.py (8010, Bearer auth) │
   │ outbound WSS│    └─ MCP endpoint ─► mcp-endpoint-server (8004)            │
   ▼            │                              ▲                               │
 Funnel 443     │        ┌─────────────────────┴───────────────┐               │
 (Tailscale)    │  Codex / Claude Code  (MCP client -> 8010)    │               │
                │    robot_say / robot_status / codex_query ... │               │
                └────────────────────────────────────────────────┘              │
```

- The gateway supports two transports: `--http` for the xiaozhi SERVER_MCP and
  Claude Code (HTTP MCP); `--stdio` for Codex CLI / other stdio clients.
- HTTP mode enforces `Authorization: Bearer <token>` (fail-closed); only
  `/healthz` is exempt.

## 3. File Inventory

| Path | Description |
|---|---|
| gateway/fusion_gateway.py | Fusion gateway main program (single file, no framework dependency) |
| gateway/config.json | Real config (OTA/MAC/health key/token/port) |
| gateway/agents_core.py | Multi-agent core (agy/pi/claude/codex) + event/confirm storage |
| gateway/run_gateway.ps1 / stop_gateway.ps1 | Gateway start/stop |
| gateway/watchdog_gateway.ps1 | Gateway watchdog (checks every 2 min, auto-restarts) |
| gateway/fusion_tray.ps1 | System tray status tool (gateway/MCP/robot 3-color) |
| gateway/install_autostart.ps1 | One-click: gateway autostart + watchdog + tray |
| docker/fusion-gateway.yaml | MCP Toolkit server definition (remote + streamable-http + Bearer) |
| docker/mcp-toolkit-profile.json | Profile `stackchan` export (for migration) |
| docker/host-executor.py | Windows host executor (for gateway calling local CLIs) |
| docker/run_executor.ps1 / install_executor_task.ps1 | Executor start + autostart |
| docker/MCP-Toolkit接入说明.md | Full Toolkit setup/verification docs |
| gateway/守护与托盘说明.md | Watchdog & tray usage docs |
| server/.mcp_server_settings.json | SERVER_MCP config for xiaozhi (streamable-http) |
| server/deploy_server_mcp.ps1 | SERVER_MCP deploy (backup→replace→restart→verify/rollback) |
| server/deploy_fusion_push.ps1 | One-click: push patch mount + fusion_secret + SERVER_MCP + rebuild |
| server-patch/core/*.py | Server patches (connection registry / http_server /api/push) |
| server-patch/docker-compose.fusion.yml | Patch overlay mount (with main compose) |
| server/prompt_patch.md | Prompt patch: check pending messages after wake-up |
| scripts/verify_connectivity.py | Layered connectivity verification |
| scripts/stop_legacy_bridge.ps1 | Stop deprecated legacy bridge.js |
| tests/test_gateway.py | Gateway self-test (stdio JSON-RPC) |
| firmware/remote_wakeup_v2.md | v2 proactive speech analysis (A/B/C) |
| package-stackchan.zip | Full migration package (firmware + PC side + README) |

## 4. Deployment Steps

```powershell
# 1. Start the gateway
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\gateway\run_gateway.ps1

# 2. Gateway self-test
python <PROJECT_DIR>\tests\test_gateway.py

# 3. Deploy to xiaozhi server (SERVER_MCP register + push patch /api/push,
#    auto stop/start containers, with backup & rollback)
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\server\deploy_server_mcp.ps1

# 4. (Optional) prompt patch so the robot proactively fetches messages after wake-up
#    Follow server/prompt_patch.md then restart the container

# 5. Connectivity verification
python <PROJECT_DIR>\scripts\verify_connectivity.py
```

## 5. Agent-Side Integration

**Recommended (current)**: unified Docker MCP Toolkit integration — see section 9.
Client config is written automatically by `docker mcp client connect`:

- Codex: `~/.codex/config.toml` → `[mcp_servers.MCP_DOCKER]`
- Claude Code: `~/.claude.json` → `MCP_DOCKER`
- VS Code: `<project root>/.vscode/mcp.json` → `MCP_DOCKER`

**Direct connection (alternative)**:

- Claude Code:
  ```
  claude mcp add --transport http fusion http://127.0.0.1:8010/mcp
  ```
  (if the CLI needs headers: configure headers Authorization: Bearer YOUR_GATEWAY_TOKEN)
- Codex CLI (~/.codex/config.toml):
  ```toml
  [mcp_servers.fusion]
  command = "python"
  args = ["<PROJECT_DIR>/gateway/fusion_gateway.py", "--transport", "stdio"]
  ```
- Note: Codex has been switched back to the CLI version (0.146.0, can run in the
  background); the store-version Access-denied problem is resolved.

## 6. Troubleshooting

| Symptom | Check |
|---|---|
| Server log `服务端MCP客户端已连接，可用工具: []` | gateway not started / container can't reach 8010 / token mismatch |
| `unhandled errors in a TaskGroup` | old `type:"ws"` config still present (replaced in this scheme); or gateway unreachable |
| Robot hears nothing | run verify_connectivity.py first, then manually wake and talk |
| Want to stop the old bridge | scripts/stop_legacy_bridge.ps1 -Kill (guard may restart it, stop guard too) |
| Gateway unreachable after Tailscale reconnect | container connects to YOUR_TAILSCALE_IP:8010, ensure the Tailscale IP hasn't changed |

## 6.5 Deployment Status (verified 2026-08-01)

| Item | Status |
|---|---|
| xiaozhi.me cloud STACK agent | ✅ device ID YOUR_DEVICE_ID bound, agent STACK |
| xiaozhi-mcp bridge (wss) | ✅ running, 8 tools, heartbeat OK (Ping every 60s) |
| Fusion gateway (8010, Bearer auth) | ✅ running, /healthz 200, 11 tools |
| Docker MCP Toolkit | ✅ profile stackchan loads fully, 19 tools visible |
| Codex / Claude Code / VS Code clients | ✅ all connected (MCP_DOCKER) |
| Agent probe | ✅ claude 2.1.220 / codex 0.146.0 / agy 1.1.9 / pi 0.80.3 |
| End-to-end tool call | ✅ agent_query(pi, "1+1") → 2 |
| Robot voice test | ✅ "check codex status" → robot replies "codex status OK" |
| Gateway watchdog + tray | ✅ watchdog auto-restarted in ~6s after kill; tray 3-color OK |

Three pitfalls fixed during deployment:
1. .ps1 Chinese garbled → all scripts saved as UTF-8 BOM.
2. .config.yaml once read/corrupted as ANSI → restored from backup, deploy script
   now uses .NET UTF-8 read/write.
3. Two FastMCP 1.28 pitfalls: the outer wrapper must propagate the inner
   lifespan; transport security rejects non-localhost Host headers (421) by
   default → allowed_hosts added.

Also fixed: MCP Toolkit profile missing `description` caused UI "Failed to load
profiles" → added description/icon/readme/metadata, loads normally now.

## 7. Known Limitations

- v1 agent→robot is "queue + wake-up speech", not interruptive push; true active
  speech: see firmware/remote_wakeup_v2.md.
- MQTT remote wake (official path) is not feasible on this network (AP isolation
  + Funnel doesn't support UDP/1883) unless MQTT goes public.
- M5Stack official firmware already ships 8 device tools (volume/screen/LED/
  camera self.camera.take_photo etc.), merged into the function list.

---

## 8. Multi-Agent Two-Way Conversation (v2, 2026-08-01)

Bidirectional communication between the robot and 4 local agents plus their VS
Code plugins: **agy (Antigravity CLI) / pi (pi-coding-agent) / claude (Claude
Code CLI) / codex (Codex CLI)**.

### Architecture

```
Robot (cloud STACK agent / self-hosted docker, both work)
   │ voice
   ▼
xiaozhi LLM ──tool call──► xiaozhi-mcp bridge (cloud) or fusion gateway (self-hosted)
                              │ agents_core.py (shared event/confirm storage)
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
   agent_query            agent_pending         agent_confirm
   (agy/pi/claude/codex   (events to speak +     (voice answer written back)
    headless, logs done)   pending questions)
                              ▲
   agent-side events ──► fusion gateway POST /api/agent_event (hooks/wrappers)
   claude permission confirm ──► agents/confirm_mcp.py (permission-prompt-tool)
```

### Tool List (cloud bridge 8 / gateway 11)

| Tool | Description |
|---|---|
| agent_status(agent=all) | 4 agents' CLI availability / running processes / pending confirms / recent events |
| agent_query(agent, task) | Headless run of agy/pi/claude/codex, result → event + outbox |
| agent_pending(clear) | Robot-side: read pending events and confirmation questions |
| agent_confirm(agent, answer) | Write the user's voice answer back to the waiting agent |
| claude_query / codex_query / docker_status / robot_say / robot_pending | v1 tools kept |

### Confirmation Loop (claude permission → robot → voice answer → claude)

- `agents/confirm_mcp.py`: MCP server used as claude `--permission-prompt-tool`
- `agents/claude_run.py`: wrapper that runs `claude -p` with confirm MCP
- `agents/claude_hook.py` + `install_claude_hooks.ps1`: installs Stop/SessionEnd/
  Notification hooks into ~/.claude/settings.json; claude sessions in VS Code also report
- Flow: claude needs permission → confirm_mcp registers the question → gateway
  queues (self-hosted can push) → after wake-up the robot's LLM reads
  agent_pending aloud → user answers → LLM calls agent_confirm → answer written
  to reply_file → confirm_mcp returns allow/deny → claude continues

### Usage

```powershell
# claude with confirmation loop
python <PROJECT_DIR>\agents\claude_run.py "task description" "working dir"
# install hooks for VS Code / terminal claude sessions
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\agents\install_claude_hooks.ps1
```

### Cloud STACK Agent Persona (already pasted into the xiaozhi.me console, wake word "A Song")

```
My name is A Song (阿松), a desktop companion AI, cute and lively, replies in
1-2 short sentences, no more than 50 characters.
Tool rules:
- User asks about agent status on the computer / who is available: call agent_status
- User asks agy/pi/claude/codex to do something: call agent_query, reply "executing" at once
- User asks "any messages/todos/who's looking for me": call agent_pending and read aloud
- User answers an agent's pending question: call agent_confirm(agent, answer)
- User asks "is the result ready": call agent_result_check or agent_pending
- Simple Q&A / chit-chat / device control (nod/light/photo): answer directly or use device tools
[LED ring feedback rules]
- While the user speaks / you are listening: call self.led.set_color, r=0, g=120, b=255 (blue)
- Before starting to speak a reply: call self.led.set_color, r=0, g=255, b=90 (green)
- After speaking: call self.led.auto (standby warm orange)
- If the user asks for a specific color: set that color
- Important: call each stage only once; ignore LED tool failures
```

> Note: current firmware is an esp32-based modified version (wake word "A Song"
> + LED status light); flash files in `firmware/` (merged-binary.bin + xiaozhi.bin).

### Known Limitations

- Cloud link has no push channel: agent events queue in the gateway, the robot
  reads them via agent_pending **after wake-up** (non-interruptive); the
  self-hosted link can push with robot_say.
- Confirmation loop fully supports claude (permission-prompt-tool); agy/pi
  headless queries work, interactive confirmation awaits their CLI support;
  codex CLI queries work, confirmation mechanism awaits official support.
- pi must use `--no-context-files` and workdir=user home (some directories error
  "content is not iterable").
- VS Code plugins: claude extension reports via hooks; pi extension has no hooks yet.

---

## 9. Docker MCP Toolkit Unified Integration (2026-08-01)

Use the **MCP Toolkit** built into Docker Desktop (`docker mcp` CLI) as a unified
MCP gateway, exposing the local fusion-gateway (localhost:8010, 11 tools) to
**Codex / Claude Code / VS Code** — clients just connect to one `MCP_DOCKER` entry.

### 9.1 Structure

```
Codex Desktop / Claude Code CLI / VS Code MCP
        │  stdio: docker mcp gateway run --profile stackchan
        ▼
Docker MCP Gateway (Toolkit, profile=stackchan)
        │  streamable-http: http://localhost:8010/mcp + Bearer
        ▼
fusion_gateway.py (Windows, :8010, 11 tools)
        │  agent_query / agent_status / agent_pending / agent_confirm ...
        ▼
agents_core.py → agy / pi / claude / codex CLI
```

### 9.2 Key Files

| Path | Description |
|---|---|
| pc/docker/fusion-gateway.yaml | MCP Toolkit server definition (remote + streamable-http + Bearer) |
| pc/docker/mcp-toolkit-profile.json | Profile `stackchan` export (for migration) |
| pc/docker/MCP-Toolkit接入说明.md | Full setup/verification steps |
| pc/gateway/watchdog_gateway.ps1 | Gateway watchdog (every 2 min, auto-restart) |
| pc/gateway/fusion_tray.ps1 | System tray status tool (gateway/MCP/robot 3-color) |
| pc/gateway/守护与托盘说明.md | Watchdog & tray usage |

### 9.3 Requirements

- Docker Desktop 4.62+ (verified on 4.84.0)
- User-level env var: `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1`
  (allows the Toolkit to reach the local fusion-gateway over http; the Toolkit
  enforces https by default)
- fusion-gateway must be running: `pc/gateway/run_gateway.ps1`

### 9.4 Migrating to a New Machine

```powershell
# 1) Install dependencies
pip install mcp uvicorn starlette websockets python-dotenv

# 2) Copy pc/docker/fusion-gateway.yaml to the new machine
Copy-Item .\pc\docker\fusion-gateway.yaml $HOME\.docker\mcp\catalogs\

# 3) Import the profile (contains endpoint/Bearer config)
docker mcp profile import .\pc\docker\mcp-toolkit-profile.json

# 4) Set env var and connect clients
[Environment]::SetEnvironmentVariable('DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS','1','User')
docker mcp client connect codex --global --profile stackchan
docker mcp client connect claude-code --global --profile stackchan
# (VS Code, in project root) docker mcp client connect vscode --profile stackchan

# 5) Start gateway + watchdog + tray
powershell -NoProfile -ExecutionPolicy Bypass -File .\pc\gateway\install_autostart.ps1
```

> Note: the profile carries a Bearer token (matching gateway/config.json's
> auth_token); keep both consistent after moving. Fill in your own MCP_ENDPOINT
> in xiaozhi-mcp/.env on the new machine.

### 9.5 Watchdog & Tray (enabled on this machine)

Two scheduled tasks (registered by install_autostart.ps1, all run silently with
`-WindowStyle Hidden`):

| Task | Trigger | Content |
|---|---|---|
| StackChan-FusionGateway | at logon | start gateway (silent) |
| StackChan-FusionTray | at logon | system tray status tool |

The tray polls every 5s: gateway /healthz, MCP profile, robot bridge heartbeat.
Icon: green=all OK / orange=partial / red=gateway offline; balloon on state
change; right-click for details, restart gateway, exit tray. See
gateway/守护与托盘说明.md.

**Watchdog is built into the tray**: if the gateway goes offline it is silently
restarted (30s debounce), with no scheduled task required — so no periodic
PowerShell windows pop up. See gateway/守护与托盘说明.md.

### 9.6 Backup & Packaging

`package_stackchan.py` generates `package-stackchan/` + `package-stackchan.zip`
(firmware 7 bins + PC-side gateway/xiaozhi-mcp/agents/docker + README), with
secrets replaced by placeholders. Use the zip for migration, see 9.4.

---

## 10. Sensitive-Info Replacement (read before publishing)

This repository is a **sanitized release**: all tokens / keys / activation
codes / device identifiers / internal addresses are replaced with placeholders.
Fill in your real values before deploying. **Never** commit real secrets to GitHub.

| Placeholder | Meaning | Location |
|---|---|---|
| `YOUR_GATEWAY_TOKEN` | Fusion gateway Bearer auth token | gateway/config.json.example, docker/fusion-gateway.yaml, docker/mcp-toolkit-profile.json |
| `YOUR_HEALTH_KEY` | xiaozhi MCP endpoint health key | gateway/config.json.example |
| `YOUR_FUNNEL_DOMAIN.ts.net` | Tailscale Funnel domain | gateway/config.json.example |
| `YOUR_TAILSCALE_IP` | Tailscale LAN IP | server/.mcp_server_settings.json |
| `AA:BB:CC:DD:EE:FF` | Robot MAC address | gateway/config.json.example |
| `YOUR_DEVICE_ID` | xiaozhi.me device ID | README deployment status table |
| `YOUR_TOKEN_HERE` | xiaozhi.me MCP endpoint JWT | xiaozhi-mcp/.env.example |
| `<PROJECT_DIR>` | Local absolute project path | .ps1 scripts / mcp_config.json |
| `<USER_HOME>` | Local user home directory | some scripts |

### Replacement Steps

1. Copy `gateway/config.json.example` to `gateway/config.json` and fill in:
   `ota_url` (your Funnel domain), `robot_mac`, the key in
   `endpoint_health_url`, `auth_token` (a strong random string of your own).
2. Copy `xiaozhi-mcp/.env.example` to `xiaozhi-mcp/.env` and fill in the
   xiaozhi.me console MCP endpoint (with JWT).
3. Keep the Bearer in `docker/fusion-gateway.yaml` and
   `docker/mcp-toolkit-profile.json` consistent with config.json's `auth_token`.
4. Run the MCP Toolkit migration per section 9.4.

### Publish Checklist

- [ ] Global search for `YOUR_` placeholders — all replaced with real values
- [ ] Do not add `.env` / `config.json` / `*.log` to git (see .gitignore)
- [ ] Run `git diff --cached` before pushing to verify no accidental secrets

---

## 11. Acknowledgements

This project stands on the shoulders of the following open-source projects and
services. Thanks to their authors:

| Project | Author | Used for |
|---|---|---|
| [Stackchan-HtSz](https://github.com/mo-hantang/Stackchan-HtSz) | [mo-hantang](https://github.com/mo-hantang) | Basis of the local HtSz firmware (custom stack / servo control) |
| [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | [xinnan-tech](https://github.com/xinnan-tech) | xiaozhi protocol server (basis of SERVER_MCP / push patch) |
| [StackChan](https://github.com/hylarucoder/StackChan) | [hylarucoder](https://github.com/hylarucoder) | StackChan firmware modification reference (servo/camera/wake word) |
| [stackchan-xiaozhi-firmware](https://github.com/heavenchenggong/stackchan-xiaozhi-firmware) | [heavenchenggong](https://github.com/heavenchenggong) | Base version of the local firmware (wake word + Servo MCP + always-on) |
| [stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge) | [heavenchenggong](https://github.com/heavenchenggong) | Robot ↔ Claude Code bridge architecture reference |
| [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) | [migratorywhale](https://github.com/migratorywhale) | Robot MCP tool capability research |
| [mcp-calculator](https://github.com/78/mcp-calculator) | [78](https://github.com/78) | MCP Server example (connecting to xiaozhi.me endpoint) |
| [xiaozhi.me](https://xiaozhi.me) | xiaozhi team | Cloud agent platform / MCP endpoint |

Thanks again to the authors and communities of the above projects.

> Disclaimer: this repository is a personal experiment and has no affiliation
> with the acknowledged authors.

---

## 12. Keywords 关键词 キーワード

**English**: stackchan · m5stack · esp32 · xiaozhi · mcp · model-context-protocol · ai-agent · claude-code · codex · voice-assistant · iot · robot · llm

**中文**: 桌面机器人 · 语音助手 · 大模型 · MCP · 智能体 · 双向通话 · ESP32 · M5Stack · 小智 · Codex · Claude Code · 物联网 · 树莓派(可选)

**日本語**: スタックチャン · デスクトップロボット · 音声アシスタント · LLM · MCP · エージェント · ESP32 · M5Stack · 小智 · Codex · Claude Code · IoT · ロボット
