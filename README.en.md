# StackChan Fusion — Voice-Driven Desktop Robot × Local AI Agents

> **日本語版**: [README.ja.md](README.ja.md) · **中文版**: [README.md](README.md)

Turn a **StackChan** (M5Stack CoreS3 desktop robot) into a voice-controlled
companion that talks to your local AI agents — **Antigravity (agy), pi-coding-agent,
Claude Code, and Codex** — bidirectionally.

## What It Does

- **Robot → Agent**: Speak to the robot; the cloud LLM (via xiaozhi.me) calls
  local tools (`agent_query`, `agent_status`, `agent_pending`, `agent_confirm`)
  that run your local agent CLIs and read back the result.
- **Agent → Robot**: Agent events are queued; the robot reads them after wake-up
  (`agent_pending`) or, on the self-hosted server link, receives them via
  proactive push (`robot_say`).
- **Desktop integration**: Docker MCP Toolkit exposes all 11 gateway tools to
  Codex, Claude Code, and VS Code through a single `MCP_DOCKER` entry.

## Architecture

```
StackChan robot (ESP32, custom firmware, wake word "A Song")
        │  voice (xiaozhi.me cloud STACK agent)
        ▼
xiaozhi.me MCP endpoint (wss) ──► xiaozhi-mcp bridge (8 tools)
                                        │ agents_core.py
                                        ▼
                              fusion-gateway.py (:8010, 11 tools)
                                        │
        ┌───────────────────────────────┼──────────────────────────┐
        ▼                               ▼                          ▼
   agent_query                    agent_pending              agent_confirm
   (agy/pi/claude/codex)          (pending events)           (voice replies)
        ▲
        └── agent hooks ──► POST /api/agent_event

Codex / Claude Code / VS Code ──► Docker MCP Toolkit (profile: stackchan)
                                      └──► fusion-gateway (:8010/mcp)
```

## Components

| Path | Purpose |
|---|---|
| `gateway/fusion_gateway.py` | Fusion gateway (FastMCP, HTTP + stdio), 11 tools |
| `gateway/agents_core.py` | Multi-agent core: status/query/pending/confirm |
| `gateway/watchdog_gateway.ps1` | Gateway watchdog (auto-restart on crash) |
| `gateway/fusion_tray.ps1` | System tray monitor (gateway/MCP/robot status) |
| `docker/fusion-gateway.yaml` | MCP Toolkit server definition |
| `docker/mcp-toolkit-profile.json` | MCP Toolkit profile export (`stackchan`) |
| `docker/host-executor.py` | Windows host executor (for containerized gateway) |
| `xiaozhi-mcp/server.py` + `mcp_pipe.py` | Cloud bridge to xiaozhi.me MCP endpoint |
| `agents/` | Claude hooks, confirm MCP, claude_run wrapper |
| `firmware/post-fw-v1.0.0-led/` | Custom firmware binaries (wake word + LED state) |

## Tools (gateway 11 / cloud bridge 8)

| Tool | Description |
|---|---|
| `agent_status(agent)` | CLI availability, running processes, pending confirmations |
| `agent_query(agent, task)` | Run agy/pi/claude/codex headlessly, log result event |
| `agent_pending(clear)` | Read pending agent events and confirmation questions |
| `agent_confirm(agent, answer)` | Write back the user's voice answer |
| `claude_query` / `codex_query` | Direct single-agent queries (v1 tools) |
| `robot_say` / `robot_pending` | Queue / read robot speech |
| `docker_status` / `ws_probe` / `robot_status` | Diagnostics |

## Quick Start

```powershell
# 1. Install dependencies
pip install mcp uvicorn starlette websockets python-dotenv

# 2. Configure gateway (see Section: Sensitive-Info Replacement)
Copy-Item .\gateway\config.json.example .\gateway\config.json
Copy-Item .\xiaozhi-mcp\.env.example .\xiaozhi-mcp\.env

# 3. Start gateway + tray (auto-start tasks registered)
powershell -NoProfile -ExecutionPolicy Bypass -File .\gateway\run_gateway.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\gateway\install_autostart.ps1

# 4. Connect MCP Toolkit clients
docker mcp profile import .\docker\mcp-toolkit-profile.json
docker mcp client connect codex --global --profile stackchan
docker mcp client connect claude-code --global --profile stackchan
```

> Windows only. Requires Docker Desktop 4.62+ (MCP Toolkit) and the
> `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1` user env var (allows the Toolkit
> to reach the local gateway over http).

## Sensitive-Info Replacement (before deploying)

This repository is a **sanitized release**. All tokens, keys, device IDs and
internal addresses are placeholders — fill in your own values:

| Placeholder | Meaning | Where |
|---|---|---|
| `YOUR_GATEWAY_TOKEN` | Gateway Bearer token | `gateway/config.json.example`, `docker/*.yaml|json` |
| `YOUR_HEALTH_KEY` | MCP endpoint health key | `gateway/config.json.example` |
| `YOUR_FUNNEL_DOMAIN.ts.net` | Tailscale Funnel domain | `gateway/config.json.example` |
| `YOUR_TAILSCALE_IP` | Tailscale LAN IP | `server/.mcp_server_settings.json` |
| `AA:BB:CC:DD:EE:FF` | Robot MAC address | `gateway/config.json.example` |
| `YOUR_DEVICE_ID` | xiaozhi.me device ID | README status table |
| `YOUR_TOKEN_HERE` | xiaozhi.me MCP JWT | `xiaozhi-mcp/.env.example` |
| `<PROJECT_DIR>` / `<USER_HOME>` | Local absolute paths | scripts / configs |

Never commit `.env`, `config.json`, or `*.log`.

## Acknowledgements

- [Stackchan-HtSz](https://github.com/mo-hantang/Stackchan-HtSz) by [mo-hantang](https://github.com/mo-hantang)
- [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) by [xinnan-tech](https://github.com/xinnan-tech)
- [StackChan](https://github.com/hylarucoder/StackChan) by [hylarucoder](https://github.com/hylarucoder)
- [stackchan-xiaozhi-firmware](https://github.com/heavenchenggong/stackchan-xiaozhi-firmware) by [heavenchenggong](https://github.com/heavenchenggong)
- [stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge) by [heavenchenggong](https://github.com/heavenchenggong)
- [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) by [migratorywhale](https://github.com/migratorywhale)
- [mcp-calculator](https://github.com/78/mcp-calculator) by [78](https://github.com/78)
- [xiaozhi.me](https://xiaozhi.me) — cloud agent platform / MCP endpoint

## Keywords

`stackchan` · `m5stack` · `esp32` · `xiaozhi` · `mcp` · `model-context-protocol`
· `ai-agent` · `claude-code` · `codex` · `voice-assistant` · `iot` · `robot`

## License & Disclaimer

Personal experiment project. Not affiliated with any of the acknowledged authors.
