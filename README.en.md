# StackChan Fusion

> **v08.06 (2026-08-06):** Announcement pipeline reworked — µ-law over EMQX MQTT with a
> msg_uid idempotency + firmware-ACK closed loop; single-worker FIFO push; agent aliases +
> fail-fast probe; VS Code integration; interactive Claude permission broadcast.
> `README.md` (zh) is authoritative for the latest state.

Drive local AI agents — **codex / claude / agy / pi** — by voice from a
**StackChan desktop robot** (M5Stack CoreS3): check status, dispatch tasks,
announce results, confirm by voice.

> Core approach (2026-08-03): **cloud link + wake-up announcements**. The robot
> runs the xiaozhi.me cloud agent and, on every wake-up, automatically checks for
> pending messages and reads them one by one. Robot-dispatched tasks run in the
> **agent's own visible window**; results flow back through hooks. The
> self-hosted xiaozhi-esp32-server link is kept as a backup.

## Architecture

```
Robot (M5Stack CoreS3, firmware v1.0.2-micfix, wake word "A Song/阿松")
   │ voice (ASR/LLM/TTS in the xiaozhi.me cloud)
   ▼
xiaozhi.me cloud agent (STACK, prompt in prompt-阿松-v3.md)
   │ MCP (wss://api.xiaozhi.me/mcp)
   ▼
xiaozhi-mcp cloud bridge (mcp_pipe.py + server.py, on this PC)
   │ agent_status / agent_query / agent_pending / agent_confirm / agent_result_check ...
   ▼
Fusion gateway fusion_gateway.py (:8010, Bearer auth)
   │
   ├── codex   (hooks: task start/done/needs-approval → robot)
   ├── claude  (hooks + confirm_mcp confirmation loop)
   ├── agy     (Antigravity fusion hooks, CLI reported as agent=agy)
   └── pi      (extension hooks-bridge.ts)
        └── robot tasks → run in the agent's own visible window (Codex-Asong / ClaudeCode-Asong / ...)
```

Two links:

| Link | Description |
|---|---|
| Cloud link (primary) | Robot voice via xiaozhi.me; agent events are queued and read after wake-up |
| Self-hosted link (backup) | Local docker xiaozhi-esp32-server + Tailscale Funnel; supports `robot_say` real push |

## Features

| Capability | Description |
|---|---|
| Wake-up announcements | On every wake-up the agent checks `agent_pending` first, reads messages one by one, then clears |
| Status queries | "Check XX status" → `agent_status` (availability/processes/recent events for all 4 agents, <5s) |
| Task execution | "Ask XX to do..." → `agent_query`, runs in the agent's own visible window, result flows back |
| Confirmation loop | claude permission request → robot reads it → voice answer → written back as allow/deny (full support for claude) |
| Device control | Nod/shake/turn/expression/camera/LED (status LED follows automatically in firmware) |

## Quick Start

### New PC / new robot

Full deployment steps (placeholder configs, firmware flashing, WiFi, xiaozhi.me
binding, all four agent hooks) are in **[DEPLOY.md](DEPLOY.md)**.

### Local services

```powershell
# Fusion gateway (:8010, required)
powershell -ExecutionPolicy Bypass -File gateway\run_gateway.ps1
# Cloud bridge (required when the robot uses xiaozhi.me)
powershell -ExecutionPolicy Bypass -File xiaozhi-mcp\run_bridge.ps1
# Backup-link containers (optional)
docker compose -f server\docker-compose.fusion.yml up -d
# Tray + auto-start (optional)
powershell -ExecutionPolicy Bypass -File gateway\install_autostart.ps1
```

### Verification

```powershell
python scripts\verify_connectivity.py
```

When all checks PASS: say "A Song/阿松" to wake the robot → it should announce
pending items; say "check agent status" → it reports all four agents; say
"ask codex to summarize the project" → a Codex window opens, runs, and the
robot announces the result after wake-up.

## Agent Integration

| Agent | Integration | Active reporting | Voice write-back |
|---|---|---|---|
| codex | `~/.codex/hooks.json` → `agents/codex_hook.py`; `config.toml` `bypass_hook_trust=true`, `[windows] sandbox='unelevated'` | ✅ desktop + CLI | ❌ (confirm in the codex UI) |
| claude | `~/.claude/settings.local.json` hooks → `agents/claude_hook.py` (local file is not overwritten by ccswitch model switching); visible-window runs report completion via `agents/claude_visible_run.py`; `agents/confirm_mcp.py` | ✅ | ✅ full loop |
| agy / Antigravity | `~/.gemini/config/hooks.json` `fusion` block → `agents/antigravity_hook.py` | ✅ CLI as agent=agy | ❌ |
| pi | `~/.pi/agent/extensions/hooks-bridge.ts` → gateway | ✅ | ❌ |
| vscode | `agents/vscode_hook.py` reports done on task/terminal end; voice dispatch is **refused** (prevents `code -r <task>` opening the text as a file) | ✅ | ❌ |

Tasks run in the agent's own visible console window (titles `Codex-Asong` /
`ClaudeCode-Asong` / `Antigravity-Asong` / `pi-Asong`; scripts in
`gateway/state/visible_runs/`); results are written to the gateway by each
agent's hooks and announced by the robot after wake-up.

## Robot Firmware

- Current: **v1.0.6-ttsbuf** (`firmware/post-fw-v1.0.6-ttsbuf/`)
- Base: the verified 07.31 `reference/stackchan-xiaozhi-firmware`
  (heavenchenggong lineage, includes "A Song" + LED patches; **do not use the
  HtSz main branch** — it has a boot bug)
- v1.0.6: larger TTS playback buffering (decode queue 2.4s→4.8s, playback
  headroom 2→4, backpressure instead of dropping) — targets long-announcement
  word loss
- v1.0.5: mic gain 42→36 (reduce clipping distortion)
- v1.0.4: **device-side AEC reverted** (v1.0.3 AEC caused the audio_input task
  to spin forever on CoreS3, robot unresponsive)
- Kept optimizations: wake speed (window 3000→1500ms, threshold floor 0.35→0.30);
  warm WebSocket connection (2s re-connect after drop, wake skips re-handshake)
- Previous v1.0.2-micfix: mic input gain 30→42 (fixes poor recognition);
  wake word "A Song"; post-fw layout (app @ 0x410000, 16MB)
- Upgrade: app-only flash `xiaozhi.bin @ 0x410000`, keeps config
  (`firmware/post-fw-v1.0.3-aec-wake/flash_post_fw.ps1`)
- Build: espressif/idf:v5.5.2 (5.5.4 causes black screen), flow in
  `firmware/build_fw_v103.ps1` + `build_led_ci.sh`

## Services & Ops

| Service | Port | Notes |
|---|---|---|
| Fusion gateway | 8010 | 12 MCP tools (incl. `robot_snap` camera), Bearer auth |
| xiaozhi-mcp cloud bridge | — | mcp_pipe.py + server.py, 60s heartbeat |
| xiaozhi-esp32-server (Docker) | 8000/8003 | backup link |
| mcp-endpoint-server (Docker) | 8004 | backup-link MCP endpoint |
| funnel_proxy.py | 8090 | backup route (auto-start + 5-min self-heal) |
| System tray | — | status monitor + gateway watchdog (single-instance guard) + queue-ops menu (view/clear) |

Watchdog and scheduled tasks all launch via `wscript.exe` + VBS hidden
launchers (no flashing windows); `install_autostart.ps1` registers them.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "check agent status" times out | gateway/bridge down; probes now cached 120s + parallel (<5s) |
| codex window reports Access denied | `~/.codex/config.toml` `[windows] sandbox='unelevated'`; never add `--sandbox workspace-write` |
| Garbled Chinese tasks | hooks read UTF-8; `mcp_pipe` child `PYTHONUTF8=1` (fixed; restart codex desktop to apply) |
| Robot reads stale results | `agent_result_check` only returns results <30 min old (fixed) |
| Two tray icons | `fusion_tray.ps1` single-instance guard (fixed) |
| Robot does not announce | confirm it is awake and the cloud prompt is v3 (`prompt-阿松-v3.md`) |

## Known Limits

- The cloud link has **no interruptive push**: agent events are queued and read
  via `agent_pending` after wake-up; only the self-hosted link supports real
  `robot_say` push.
- Internal sessions of the Codex / Antigravity desktop apps and VS Code
  extension panels **cannot be injected externally**; robot tasks run in the CLI
  windows, while plugin sessions still report events via hooks.
- The confirmation loop is complete only for claude
  (`--permission-prompt-tool` + `confirm_mcp`); codex/agy/pi only report
  "needs approval" — confirm in the agent UI.
- End-to-end voice latency is ~1.5–2.5s (cloud ASR/LLM/TTS); acceptable for
  non-interruptive announcements.

## Version History

### v08.07 (2026-08-07)

- **Phase 8.1 motion engine**: firmware `done/error` → servo Nod, `question` → head
  tilt +15° (TiltAsk); idle wandering head-move interval 4s → **20s**
  (`kIdleScanIntervalUs`, verified on device).
- **Phase 8.2 CoreS3 vision MCP**: gateway `robot_snap` (12 tools); firmware takes a
  JPEG photo, streams chunks over MQTT (`stackchan/{mac}/photo`, QoS1), gateway
  reassembles and validates JPEG magic + total length; 3/3 real-device shots OK.
- **Claude hooks survive model switching**: `install_claude_hooks.ps1` now writes
  `~/.claude/settings.local.json` (higher priority, not clobbered by ccswitch);
  four hooks injected + self-heal hint.
- **VS Code voice dispatch refused**: `agents_core.query()` returns
  "VS Code 暂不支持语音派发任务" for agent `vscode`/`code` — no more accidental
  `code -r <task>` file-opens; manual tasks still report via `vscode_hook.py`.
- **Claude stream-interruption fallback**: empty summary now reports
  "Claude 会话结束(响应可能中断, 详见电脑)" instead of silently dropping.
- **codex hooks cleanup**: removed 15 PromLight zombie entries from
  `~/.codex/hooks.json` (backup `hooks.json.bak-20260807-110621`); only the 5
  codex_hook events remain.
- **Sanitization**: public copies no longer contain the Tailscale IP
  `<TAILSCALE_IP>` or real local paths.

### v08.06 (2026-08-04)

- Firmware v1.0.6-ttsbuf (flashed): TTS playback buffer 2.4s→4.8s, playback
  headroom 2→4, backpressure queueing (no drops)
- Firmware v1.0.5: mic gain 42→36
- **Device-side AEC reverted**: v1.0.3 AEC made the audio_input task spin forever
  on CoreS3 (task_wdt, robot unresponsive/rebooting); located via addr2line in the
  dios_ssp AEC DSP; VAD(WebRTC) pipeline restored; wake-speed and warm-connection
  fixes kept (2s re-connect after drop)
- Prompt v3 finalized (user-approved): reply language follows the xiaozhi.me
  preset; ASR-tolerant intent routing (infer by meaning, never treat
  "announce/status" as song search); "say that again" when unclear
- Cloud: STACK agent model `deepseek-v4-flash-ha` hit `503 No available channel`
  → switched to `qwen3.6`
- **Pending**: long announcements still choppy/word-lossy — v1.0.6 buffering +
  backpressure applied; investigating (multi-segment TTS ResetDecoder at segment
  boundaries / server burst underrun / WS drop)

### v08.05 (2026-08-04)

- Firmware v1.0.3-aec-wake (`firmware/post-fw-v1.0.3-aec-wake/`):
  - Device-side AEC (ES7210 reference input cancels speaker echo), listening
    mode switched to Realtime (barge-in, no tail truncation)
  - Wake speed: multinet detection window 3000→1500ms, threshold floor 0.35→0.30
  - Warm connection: idle WebSocket kept alive (15s→120s exponential backoff),
    wake skips the re-handshake
- Phase 5 decisions: P5-1/P5-2 (pi/agy voice-confirm loops) dropped — pi via
  VS Code, agy via Antigravity Desktop; P5-4 (cloud active push) infeasible —
  xiaozhi.me has no idle-trigger API; P5-5 (desktop session injection) infeasible —
  codex app-server daemon is Unix-only, remote-control is SSH pairing
- Rollback backup: git tag `backup-v08.04` + `backup-v08.04.zip`

### v08.04 (2026-08-04)

- Cloud bridge "auto-start + keep-alive": StackChan-CloudBridge logon task
  (wscript hidden); tray built-in bridge watchdog (silent restart within 30s
  when processes/heartbeat fail)
- Tray single-instance guard hardened (only real `-File` instances count)
- Bridge startup chain fully silent (VBS → powershell Hidden → python Hidden)
- Tray "Queue Ops" menu: show queue messages / clear queue (auto-backup) /
  clear pending confirmations

### v08.03 (2026-08-03)

- Cloud link + wake-up announcements (prompt v2, agent_pending wake-first rule)
- Firmware v1.0.2-micfix (mic gain 42, recognition fix)
- All four agent hooks live (codex/claude/agy/pi), visible-window execution
- claude visible-window completion event (`agents/claude_visible_run.py`:
  results go to both the event queue and the outbox, so wake-up announcements
  and "ask for the result" both work)
- Fixes: codex Access denied, agent_status timeout (13.9s→4.8s), garbled
  Chinese, stale results, claude missing completion, claude/pi workdir,
  double tray icon, scheduled-task window flashes
- Archive: `version.08.03/` (full package of the day)

Older: `firmware/post-fw-v1.0.0-led` (verified 07.31 build, can roll back).

## References & Acknowledgments

This project references / uses the following open-source projects. Thanks to all authors:

| Project | Author | Used for |
|---|---|---|
| [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | @xinnan-tech | self-hosted xiaozhi server (backup link) |
| [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) | @78 | device firmware upstream |
| [stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge) | @heavenchenggong | StackChan × Claude bridge firmware (source of the verified 07.31 base) |
| [StackChan-HtSz](https://github.com/mo-hantang/StackChan-HtSz) | @mo-hantang | StackChan-HtSz firmware (main branch) |
| [StackChan](https://github.com/hylarucoder/StackChan) | @hylarucoder | StackChan reference (servo/actions/LED) |
| [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) | @migratorywhale | StackChan × MCP reference |
| [pi-coding-agent](https://github.com/earendil-works/pi-coding-agent) | @earendil-works | pi coding agent |
| [mcp-calculator](https://github.com/78/mcp-calculator) | @78 | MCP tool authoring example |

Plus the official AI agent products: OpenAI Codex, Anthropic Claude Code, Google Antigravity (Gemini CLI).

## Sensitive Information

This repository contains **no real credentials**: tokens / API keys / MAC /
domains are placeholders (`YOUR_*` / `AA:BB:CC:DD:EE:FF`). Real values live
only in local `.env`, `config.json`, and docker configs. `.gitignore` excludes
all runtime-sensitive files. When deploying, replace each item per
[DEPLOY.md](DEPLOY.md) section 4.
