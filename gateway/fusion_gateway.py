#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fusion_gateway.py — StackChan x xiaozhi-esp32-server x Codex/Claude Code 融合网关
================================================================================

角色:
  1. 服务端MCP (SERVER_MCP): xiaozhi-esp32-server 通过 streamable-http 连接本网关,
     把以下工具注册给设备 LLM(DeepSeek), 机器人用语音即可驱动:
       - codex_query / claude_query : 让电脑上的 Codex / Claude Code 执行任务
       - robot_pending             : 唤醒后取待播报消息(agent 排队的消息)
       - robot_status / ws_probe   : 分层连通性自检
  2. 标准 MCP server: Codex/Claude Code 也可以作为 MCP 客户端连接本网关:
       - robot_say    : 给机器人排队一条语音播报(机器人下次唤醒时由 LLM 朗读)
       - robot_status : 连通性验证
       - codex_query / claude_query : 手动驱动 agent

传输:
  --transport stdio : 供 Codex CLI / Claude Code 以 stdio MCP 方式使用
  --transport http  : 供 xiaozhi SERVER_MCP (streamable-http) 及 Claude Code (http MCP) 使用
                      HTTP 模式强制 Bearer 认证(fail-closed), /healthz 除外

用法:
  python fusion_gateway.py --transport http --host 0.0.0.0 --port 8010
  python fusion_gateway.py --transport stdio
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import agents_core

ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "ota_url": "https://YOUR_FUNNEL_DOMAIN.ts.net/xiaozhi/ota/",
    "robot_mac": "AA:BB:CC:DD:EE:FF",
    "endpoint_health_url": "http://127.0.0.1:8004/mcp_endpoint/health?key=YOUR_HEALTH_KEY",
    "docker_container": "xiaozhi-esp32-server",
    "docker_log_lookback_minutes": 120,
    "auth_token": "YOUR_GATEWAY_TOKEN",
    "allow_codex": True,
    "allow_claude": True,
    "codex_cli": "codex",
    "claude_cli": "claude",
    "exec_cwd": ".",
    "max_output_chars": 4000,
    "max_timeout_s": 600,
    "http_host": "0.0.0.0",
    "http_port": 8010,
    "push_api_url": "http://127.0.0.1:8003/api/push",
    "push_secret": "YOUR_PUSH_SECRET",
    "push_interval_s": 5,
}

TOOL_NAMES = [
    "agent_status", "robot_status", "docker_status", "ws_probe",
    "codex_query", "claude_query", "robot_say", "robot_pending",
    "agent_query", "agent_pending", "agent_confirm",
]

STARTED_AT = time.time()
CFG: dict = {}


def log(message: str) -> None:
    try:
        log_file = Path(CFG.get("log_file", ROOT / "gateway.log"))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{_now()}] {message}\n")
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(config_path: str | None = None) -> dict:
    env_path = os.environ.get("FUSION_CONFIG", "")
    path = Path(config_path) if config_path else (Path(env_path) if env_path else (ROOT / "config.json"))
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log(f"config load error: {e}")
    if str(cfg.get("exec_cwd", ".")) == ".":
        cfg["exec_cwd"] = str(ROOT)
    cfg["log_file"] = str(ROOT / "gateway.log")
    cfg["pending_file"] = str(ROOT / "state" / "pending.jsonl")
    return cfg


# ---------------------------------------------------------------- 队列
def pending_count() -> int:
    try:
        with open(CFG["pending_file"], encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def pending_append(text: str, source: str) -> int:
    path = Path(CFG["pending_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": uuid.uuid4().hex[:8], "text": text, "source": source, "created_at": _now(),
        }, ensure_ascii=False) + "\n")
    return pending_count()


def pending_read(clear: bool) -> list[str]:
    path = Path(CFG["pending_file"])
    items: list[str] = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    items.append(f"[{obj.get('created_at', '')}] {obj.get('text', '')}")
                except Exception:
                    items.append(line)
    if clear:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
    return items




def pending_items() -> list[dict]:
    path = Path(CFG["pending_file"])
    items: list[dict] = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    items.append({"id": "", "text": line, "source": "legacy", "created_at": ""})
    return items


def pending_remove_ids(ids: set[str]) -> int:
    path = Path(CFG["pending_file"])
    if not path.exists() or not ids:
        return 0
    kept = [o for o in pending_items() if o.get("id") not in ids]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in kept:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return len(kept)


def push_send(text: str) -> tuple[bool, str]:
    """调用 xiaozhi 服务器 /api/push, 让机器人立即播报。"""
    url = CFG.get("push_api_url", "")
    if not url:
        return False, "未配置 push_api_url"
    secret = str(CFG.get("push_secret", "") or "")
    payload = json.dumps({"mac": CFG.get("robot_mac", ""), "text": text, "secret": secret}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={"Content-Type": "application/json", "User-Agent": "fusion-gateway/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            return bool(body.get("ok")), body.get("error") or "ok"
    except Exception as e:
        return False, str(e)


def _drain_pending() -> tuple[int, int]:
    """把待播报消息逐条推给机器人; 返回 (成功数, 失败数)。"""
    items = pending_items()
    if not items:
        return 0, 0
    pushed_ids: set[str] = set()
    fail = 0
    for o in items:
        text = str(o.get("text", "")).strip()
        if not text:
            pushed_ids.add(o.get("id", ""))
            continue
        ok, err = push_send(text)
        if ok:
            log(f"push ok: {text[:80]}")
            pushed_ids.add(o.get("id", ""))
        else:
            fail += 1
            log(f"push fail: {err} :: {text[:80]}")
    removed = pending_remove_ids(pushed_ids) if pushed_ids else 0
    return len(pushed_ids), fail


def _push_loop() -> None:
    while True:
        try:
            interval = int(CFG.get("push_interval_s", 5) or 0)
            if interval > 0:
                _drain_pending()
                time.sleep(interval)
            else:
                time.sleep(1)
        except Exception as e:
            log(f"push loop error: {e}")
            time.sleep(5)

# ---------------------------------------------------------------- 工具函数
def http_get(url: str, timeout: float = 15.0) -> tuple[int | None, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fusion-gateway/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except Exception as e:
        return None, str(e)


def _create_no_window() -> int:
    if platform.system() == "Windows":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def resolve_cli(name: str) -> str:
    return shutil.which(name) or name


def run_cli(argv: list[str], timeout_s: int, cwd: str) -> dict:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, cwd=cwd, stdin=subprocess.DEVNULL, creationflags=_create_no_window(),
        )
        return {"ok": p.returncode == 0, "returncode": p.returncode, "stdout": p.stdout or "", "stderr": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"执行超时(>{timeout_s}s)"}
    except Exception as e:
        return {"ok": False, "returncode": -2, "stdout": "", "stderr": str(e)}


def run_agent_cli(cli_name: str, argv: list[str], timeout_s: int) -> dict:
    """运行 node/npm 系 CLI。Windows 上 .cmd/.bat 走 cmd /c, 其余直接 CreateProcess。"""
    cli = resolve_cli(cli_name)
    clean_argv = []
    for a in argv:
        clean_argv.append(str(a).replace('"', "'"))
    if cli.lower().endswith((".cmd", ".bat")):
        full = ["cmd", "/c", cli] + clean_argv
    else:
        full = [cli] + clean_argv
    return run_cli(full, timeout_s, CFG.get("exec_cwd", str(ROOT)))


def _cli_probe(cli_name: str, argv: list[str]) -> tuple[bool, str]:
    res = run_agent_cli(cli_name, argv, 20)
    if res["ok"]:
        first = (res["stdout"] or "").strip().splitlines()
        return True, (first[0][:80] if first else "ok")
    return False, (res["stderr"] or res["stdout"] or "spawn failed").strip()[:200]


def _format_cli_result(name: str, res: dict) -> str:
    maxc = int(CFG.get("max_output_chars", 4000))
    if res["ok"]:
        out = (res["stdout"] or "").strip()
        return out[:maxc] if out else f"{name} 执行成功(无输出)"
    err = (res["stderr"] or res["stdout"] or "未知错误").strip()
    return f"{name} 执行失败(rc={res['returncode']}): {err[:maxc]}"


def docker_logs_since(minutes: int, container: str) -> str:
    if shutil.which("docker") is None:
        return "[docker 不可用]"
    argv = ["docker", "logs", "--since", f"{minutes}m", container]
    res = run_cli(argv, 40, str(ROOT))
    if not res["ok"]:
        return f"[docker logs: rc={res['returncode']} {(res['stderr'] or res['stdout'])[:200]}]"
    return (res["stdout"] or "") + (res["stderr"] or "")


# ---------------------------------------------------------------- MCP 工具
mcp = FastMCP("fusion-gateway")


@mcp.tool()
def robot_status() -> str:
    """分层连通性自检: 1)Funnel OTA 2)MCP接入点 3)服务端MCP工具注册 4)机器人在线+设备工具数。Codex/Claude/机器人任一方调用。"""
    lines = []
    # 1) Funnel OTA
    status, body = http_get(CFG["ota_url"], 15)
    ota_ok = status == 200 and "websocket" in body.lower()
    lines.append(f"[1] Funnel OTA: {'PASS' if ota_ok else 'FAIL'} (HTTP {status})")
    # 2) MCP 接入点 health
    try:
        status2, body2 = http_get(CFG["endpoint_health_url"], 10)
        data = json.loads(body2) if body2 else {}
        result = data.get("result") or {}
        conns = result.get("connections") or {}
        ep_ok = result.get("status") == "success"
        lines.append(
            f"[2] MCP接入点 health: {'PASS' if ep_ok else 'FAIL'} | "
            f"tool={conns.get('tool_connections')} robot={conns.get('robot_connections')} total={conns.get('total_connections')}"
        )
    except Exception as e:
        lines.append(f"[2] MCP接入点 health: FAIL ({e})")
    # 3) 服务端 MCP 工具注册
    logs = docker_logs_since(CFG.get("docker_log_lookback_minutes", 120), CFG["docker_container"])
    reg = re.findall(r"服务端MCP客户端已连接，可用工具:\s*(\[[^\]]*\])", logs)
    last_tools = reg[-1] if reg else "[]"
    fusion_ok = "fusion" in last_tools.lower() or "codex_query" in last_tools.lower()
    lines.append(f"[3] 服务端MCP注册: {'PASS 已注册融合工具' if fusion_ok else 'FAIL 当前工具=' + last_tools}")
    # 4) 机器人在线 + 设备工具数
    mac_plain = CFG["robot_mac"].lower().replace(":", "")
    mac_colon = CFG["robot_mac"].lower()
    robot_seen = (mac_plain in logs.lower()) or (mac_colon in logs.lower())
    dev_tools = re.findall(r"客户端设备支持的工具数量:\s*(\d+)", logs)
    lines.append(
        f"[4] 机器人在线(最近{CFG.get('docker_log_lookback_minutes', 120)}min日志): {'PASS' if robot_seen else 'FAIL/离线'} | "
        f"设备工具数: {dev_tools[-1] if dev_tools else 'N/A'}"
    )
    lines.append(f"[5] 网关自身: pid={os.getpid()} pending={pending_count()}")
    return "\n".join(lines)


@mcp.tool()
def docker_status() -> str:
    """查询电脑上 Docker 容器的运行状态(容器名/状态/端口/健康)。用户问 Docker、容器、服务状态时调用。"""
    res = run_cli(["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"], 30, str(ROOT))
    if not res["ok"]:
        return f"无法查询 Docker: {(res['stderr'] or res['stdout'])[:300]}"
    out = (res["stdout"] or "").strip()
    return out[:int(CFG.get("max_output_chars", 4000))] if out else "没有运行中的容器。"

@mcp.tool()
def ws_probe() -> str:
    """连接一次 Funnel WSS 并按协议发 hello, 验证服务器接受设备连接(收到服务器 hello 即 PASS)。注意: 会短暂占用一个测试会话。"""
    try:
        import asyncio
        import websockets
    except Exception as e:
        return f"WS 探测不可用(未安装 websockets): {e}"
    status, body = http_get(CFG["ota_url"], 15)
    try:
        ws_url = json.loads(body)["websocket"]["url"]
    except Exception as e:
        return f"无法从 OTA 响应解析 websocket 地址: {e}"

    async def _probe() -> str:
        async with websockets.connect(ws_url, open_timeout=10, close_timeout=3) as ws:
            await ws.send(json.dumps({
                "type": "hello", "version": 1, "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
                "client_id": f"fusion-probe-{uuid.uuid4().hex[:8]}",
                "mac_address": "00:00:00:00:00:00",
            }))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                return "FAIL: 5秒内未收到服务器 hello"
            return f"PASS: 收到服务器响应 {str(msg)[:160]}"

    try:
        return asyncio.run(_probe())
    except Exception as e:
        return f"FAIL: {e}"


@mcp.tool()
def codex_query(instruction: str, timeout_s: int = 120) -> str:
    """在电脑上运行 Codex CLI 完成任务并返回结果。机器人端: 用户说「让Codex…」时调用。注意: 本机 Codex 为商店版时可能无法从后台启动。"""
    if not CFG.get("allow_codex", True):
        return "Codex 执行被配置禁用。"
    timeout_s = max(10, min(int(timeout_s), int(CFG.get("max_timeout_s", 600))))
    log(f"codex_query: {instruction[:200]}")
    res = run_agent_cli(CFG.get("codex_cli", "codex"), ["exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust", instruction], timeout_s)
    return _format_cli_result("Codex", res)


@mcp.tool()
def claude_query(instruction: str, timeout_s: int = 120) -> str:
    """在电脑上运行 Claude Code CLI 完成任务并返回结果。机器人端: 用户说「让Claude…」时调用。"""
    if not CFG.get("allow_claude", True):
        return "Claude Code 执行被配置禁用。"
    timeout_s = max(10, min(int(timeout_s), int(CFG.get("max_timeout_s", 600))))
    log(f"claude_query: {instruction[:200]}")
    res = run_agent_cli(CFG.get("claude_cli", "claude"), ["-p", instruction, "--output-format", "text"], timeout_s)
    return _format_cli_result("Claude Code", res)


@mcp.tool()
def robot_say(text: str) -> str:
    """给机器人排队一条待播报消息(Codex/Claude 侧调用)。push_interval_s=0 时立即推送, 否则网关每 N 秒轮询推送; 机器人离线时保留队列, 唤醒后由 robot_pending 朗读。"""
    text = str(text or "").strip()
    if not text:
        return "消息为空。"
    entry = {"id": uuid.uuid4().hex[:8], "text": text, "source": "agent", "created_at": _now()}
    path = Path(CFG["pending_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    n = pending_count()
    if int(CFG.get("push_interval_s", 5) or 0) == 0:
        ok, err = push_send(text)
        if ok:
            pending_remove_ids({entry["id"]})
            return f"已立即推送给机器人播报。(队列剩余 {max(n - 1, 0)} 条)"
        return f"已入队(当前 {n} 条)。立即推送失败: {err}"
    log(f"robot_say enqueued: {text[:120]}")
    return f"已入队(当前 {n} 条)。网关将每 {CFG.get('push_interval_s', 5)} 秒推送, 机器人离线则保留待唤醒播报。"

@mcp.tool()
def robot_pending(clear: bool = False) -> str:
    """机器人端调用: 获取待播报消息并原样朗读; 读完请再调用一次 clear=true 清除。没有消息时返回空。"""
    items = pending_read(bool(clear))
    if not items:
        return ""
    return "\n".join(items)


# ---------------------------------------------------------------- 多 agent 工具
@mcp.tool()
def agent_status(agent: str = "all") -> str:
    """查询本机各 agent(agy/pi/claude/codex) 状态: CLI 可用性/运行进程/待确认问题/最近事件。
    机器人端: 用户问「agent 状态 / 电脑上哪些 agent 能用」时调用。agent 可选 all 或具体名字。"""
    name = (agent or "all").lower()
    if name == "all":
        head = f"网关运行中(pid={os.getpid()}, 待播报 {pending_count()} 条)"
        return head + "\n" + agents_core.status_all_text()
    if name not in agents_core.AGENT_CLIS:
        return f"未知 agent: {name} (可选: {', '.join(agents_core.AGENT_CLIS)})"
    return f"网关运行中(pid={os.getpid()}, 待播报 {pending_count()} 条)\n" + agents_core.status_text(name)


@mcp.tool()
def agent_query(agent: str, instruction: str, timeout_s: int = 120) -> str:
    """在电脑上运行指定 agent(agy/pi/claude/codex) 执行任务并返回结果。
    机器人端: 用户说「让 agy/pi/claude/codex 查/做…」时调用。长任务结果自动记入待播报事件。"""
    name = (agent or "").lower()
    if name not in agents_core.AGENT_CLIS:
        return f"未知 agent: {name} (可选: {', '.join(agents_core.AGENT_CLIS)})"
    timeout_s = max(10, min(int(timeout_s), int(CFG.get("max_timeout_s", 600))))
    log(f"agent_query: {name} :: {instruction[:150]}")
    return agents_core.query(name, instruction, timeout_s)


@mcp.tool()
def agent_pending(clear: bool = False) -> str:
    """获取待播报的 agent 事件与待确认问题(如「claude 需要确认: 是否允许运行命令」)。
    机器人端: 用户问「有没有 agent 消息/待办/谁找我」或回答待确认问题时调用; 读后可再调 clear=true。"""
    return agents_core.pending_text(bool(clear))


@mcp.tool()
def agent_confirm(agent: str, answer: str) -> str:
    """把用户的语音回答回写给指定 agent 的待确认问题(确认/拒绝/补充说明)。
    机器人端: 用户对「agent 需要确认」的问题给出回答后调用。"""
    name = (agent or "").lower()
    ok, msg = agents_core.confirm_answer(name, answer)
    return msg


# ---------------------------------------------------------------- HTTP 模式
def build_http_app():
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    mcp_app = mcp.streamable_http_app()

    async def healthz(request):
        return JSONResponse({
            "status": "ok",
            "pid": os.getpid(),
            "started_at": _now(),
            "pending": pending_count(),
            "tools": TOOL_NAMES,
        })

    async def agent_event(request):
        """agent hook/包装器上报事件: {agent, event: done|question|progress|error, summary, session_id, reply_file}"""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        agent = str(data.get("agent", "")).lower()
        etype = str(data.get("event", ""))
        summary = str(data.get("summary", ""))
        reply_file = str(data.get("reply_file", ""))
        session_id = str(data.get("session_id", ""))
        if agent not in agents_core.AGENT_CLIS and agent != "antigravity":
            return JSONResponse({"error": f"unknown agent: {agent}"}, status_code=400)
        if etype == "question":
            c = agents_core.confirm_register(agent, summary, reply_file)
            pending_append(f"{agent} 需要确认: {summary[:300]}", "agent")
            return JSONResponse({"ok": True, "confirmation_id": c["id"], "pending": pending_count()})
        if etype not in ("done", "progress", "error"):
            return JSONResponse({"error": f"unknown event: {etype}"}, status_code=400)
        agents_core.events_append(agent, etype, summary, session_id)
        if etype == "done":
            pending_append(f"{agent} 任务完成: {summary[:300]}", "agent")
        return JSONResponse({"ok": True, "pending": pending_count()})

    async def confirm_status(request):
        """confirm_mcp(宿主)轮询: 按 confirmation_id 查是否已回答。"""
        cid = request.query_params.get("id", "")
        c = agents_core.confirm_get(cid)
        if not c:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "id": cid,
            "answered": bool(c.get("answered")),
            "answer": str(c.get("answer", "")),
        })

    app = Starlette(routes=[
        Route("/healthz", healthz),
        Route("/api/agent_event", agent_event, methods=["POST"]),
        Route("/api/agent/confirm_status", confirm_status),
        Mount("/", app=mcp_app),
    ])
    # 关键: 传播内层 MCP app 的 lifespan, 否则会话管理器任务组不会启动
    app.router.lifespan_context = mcp_app.router.lifespan_context

    token = str(CFG.get("auth_token") or "")
    if token:
        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path == "/healthz":
                    return await call_next(request)
                if request.headers.get("Authorization") != f"Bearer {token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(AuthMiddleware)
    return app


def _heartbeat_loop() -> None:
    while True:
        try:
            hb = Path(CFG.get("log_file", ROOT / "gateway.log")).parent / "heartbeat.json"
            hb.write_text(json.dumps({
                "pid": os.getpid(), "ts": _now(), "pending": pending_count(),
                "tools": TOOL_NAMES,
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        time.sleep(30)


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="StackChan Fusion Gateway")
    ap.add_argument("--transport", choices=["stdio", "http"], default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    global CFG
    CFG = load_config(args.config)
    CFG["_config_path"] = str(args.config) if args.config else "gateway/config.json"

    transport = args.transport or "http"
    if transport == "stdio":
        log("starting stdio transport")
        mcp.run(transport="stdio")
        return

    host = args.host or str(CFG.get("http_host", "0.0.0.0"))
    port = args.port or int(CFG.get("http_port", 8010))
    log(f"starting http transport on {host}:{port}")
    # 传输安全: 允许容器经 tailscale IP 访问, 否则 Host 校验返回 421
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{host}:{port}", "127.0.0.1:*", "localhost:*", "100.69.221.25:*"],
        )
    except Exception as e:
        log(f"transport security config error: {e}")
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    if int(CFG.get("push_interval_s", 5) or 0) > 0:
        threading.Thread(target=_push_loop, daemon=True).start()

    import uvicorn
    uvicorn.run(build_http_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
