# -*- coding: utf-8 -*-
"""agents_core.py — 多 agent 管理核心(agy/pi/claude/codex), 供 fusion_gateway 与 xiaozhi-mcp 共用。

提供:
  - agent_status(name) : CLI 可用性 + 运行进程 + 待确认数 + 最近事件
  - agent_query(name, task, timeout) : 无头执行 agy/pi/claude/codex
  - agent_pending(clear) : 待播报事件 + 待确认问题(机器人/LLM 读取)
  - agent_confirm(name, answer) : 把语音回答写回等待中的 agent
  - event_post(...) / confirm_register / confirm_answer : 内部接口
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

EVENTS_FILE = DATA_DIR / "agent_events.jsonl"
CONFIRM_FILE = DATA_DIR / "agent_confirmations.json"

# docker 方案: 宿主执行器(执行本机 CLI)。连不上时回退本地 spawn, 保证过渡期兼容。
EXECUTOR_URL = os.environ.get("FUSION_EXECUTOR_URL", "http://127.0.0.1:8091")
EXECUTOR_TOKEN = os.environ.get("FUSION_EXECUTOR_TOKEN", "")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _no_window() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _resolve(cli: str) -> str:
    return shutil.which(cli) or cli


def _run(argv: list[str], timeout: int, cwd: str) -> dict:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL,
            creationflags=_no_window(),
        )
        return {"ok": p.returncode == 0, "rc": p.returncode, "out": p.stdout or "", "err": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "out": "", "err": f"exec timeout(>{timeout}s)"}
    except Exception as e:
        return {"ok": False, "rc": -2, "out": "", "err": str(e)}


def _exec_via_http(name: str, mode: str, task: str, timeout: int) -> dict | None:
    """调宿主执行器; 失败(网络/未部署)返回 None 由调用方回退本地。"""
    try:
        body = json.dumps({"agent": name, "mode": mode, "task": task, "timeout": timeout}).encode("utf-8")
        req = urllib.request.Request(
            EXECUTOR_URL + "/exec", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {EXECUTOR_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def run_agent(name: str, task: str, timeout: int = 120, workdir: str | None = None) -> dict:
    """运行指定 agent 的无头模式。返回 {ok, out, err, rc}。"""
    via_http = _exec_via_http(name, "exec", task, timeout)
    if via_http is not None:
        return via_http
    # 回退: 本地直接 spawn
    cfg = AGENT_CLIS.get(name, {})
    cli = cfg.get("cli", name)
    cli_path = _resolve(cli)  # Windows 上解析到 .CMD 绝对路径, 避免 cmd 歧义
    args = cfg.get("exec_args", [])
    full = [cli_path] + [str(a) for a in args] + [task]
    return _run(full, timeout, workdir or cfg.get("workdir") or str(ROOT))


def probe(name: str) -> tuple[bool, str]:
    """探测 agent CLI 是否可用(返回 可用性, 版本首行)。"""
    via_http = _exec_via_http(name, "probe", "", 15)
    if via_http is not None:
        if via_http.get("ok"):
            first = (via_http.get("out") or "").strip().splitlines()
            return True, (first[0][:80] if first else "ok")
        return False, (via_http.get("err") or via_http.get("out") or "spawn failed").strip()[:150]
    cfg = AGENT_CLIS.get(name, {})
    version_args = cfg.get("version_args", ["--version"])
    cli = cfg.get("cli", name)
    cli_path = _resolve(cli)
    full = [cli_path] + version_args
    r = _run(full, 15, cfg.get("workdir") or str(ROOT))
    if r["ok"]:
        first = (r["out"] or "").strip().splitlines()
        return True, (first[0][:80] if first else "ok")
    return False, (r["err"] or r["out"] or "spawn failed").strip()[:150]


def running_processes(name: str) -> list[int]:
    """返回名字匹配的进程 PID(粗略: 按可执行名)。"""
    pids: list[int] = []
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-CimInstance Win32_Process -Filter \"Name='{name}.exe'\" | Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=10, creationflags=_no_window(),
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
    except Exception:
        pass
    return pids


# ---------------------------------------------------------------- 事件/确认 存储
def events_append(agent: str, etype: str, summary: str, session_id: str = "") -> dict:
    ev = {"id": uuid.uuid4().hex[:8], "agent": agent, "type": etype,
          "summary": (summary or "")[:500], "session_id": session_id, "ts": _now()}
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def events_read(clear: bool = False) -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    lines = [l for l in EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    items = [json.loads(l) for l in lines]
    if clear:
        EVENTS_FILE.write_text("", encoding="utf-8")
    return items[-20:]


def _confirmations() -> list[dict]:
    if not CONFIRM_FILE.exists():
        return []
    try:
        return json.loads(CONFIRM_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_confirmations(items: list[dict]) -> None:
    CONFIRM_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def confirm_register(agent: str, question: str, reply_file: str = "") -> dict:
    items = _confirmations()
    c = {
        "id": uuid.uuid4().hex[:8], "agent": agent,
        "question": (question or "")[:500], "reply_file": reply_file,
        "created": _now(), "answered": False,
    }
    items.append(c)
    _save_confirmations(items)
    return c


def confirm_pending(agent: str = "") -> list[dict]:
    items = [c for c in _confirmations() if not c.get("answered")]
    if agent:
        items = [c for c in items if c.get("agent") == agent]
    return items


def confirm_get(cid: str) -> dict | None:
    for c in _confirmations():
        if c.get("id") == cid:
            return c
    return None


def confirm_answer(agent: str, answer: str) -> tuple[bool, str]:
    items = _confirmations()
    # 回答该 agent 最近一条待确认(机器人念的是最新问题)
    for c in reversed(items):
        if not c.get("answered") and c.get("agent") == agent:
            c["answered"] = True
            c["answer"] = (answer or "")[:500]
            c["answered_at"] = _now()
            _save_confirmations(items)
            reply_file = c.get("reply_file", "")
            if reply_file:
                try:
                    Path(reply_file).write_text(answer, encoding="utf-8")
                except Exception as e:
                    return True, f"已记录回答, 但回写文件失败: {e}"
            return True, "已回复 " + agent
    return False, "没有该 agent 的待确认问题"


# ---------------------------------------------------------------- agent 定义
AGENT_CLIS: dict = {
    "claude": {"cli": "claude", "exec_args": ["-p"], "version_args": ["--version"], "workdir": str(Path.home())},
    "codex": {"cli": "codex", "exec_args": ["exec", "--skip-git-repo-check", "--sandbox", "workspace-write"],
              "version_args": ["--version"], "workdir": str(ROOT)},
    "agy": {"cli": "agy", "exec_args": ["--print"], "version_args": ["--version"], "workdir": str(ROOT)},
    "pi": {"cli": "pi", "exec_args": ["--print", "--no-session", "--no-context-files"],
           "version_args": ["--version"], "workdir": str(Path.home())},
}


def status_text(name: str) -> str:
    ok, info = probe(name)
    pids = running_processes(name)
    pend = len(confirm_pending(name))
    events = [e for e in events_read(False) if e.get("agent") == name][-3:]
    lines = [f"{name}: {'可用' if ok else '不可用'} ({info})", f"  运行进程: {len(pids)} 个", f"  待确认问题: {pend} 个"]
    for e in events:
        lines.append(f"  最近事件[{e['type']}]: {(e.get('summary') or '')[:60]}")
    return "\n".join(lines)


def status_all_text() -> str:
    return "\n".join(status_text(n) for n in AGENT_CLIS)


def pending_text(clear: bool = False) -> str:
    """机器人端调用: 返回待播报的 agent 事件 + 待确认问题。"""
    parts = []
    for ev in events_read(clear):
        parts.append(f"[{ev['agent']} {ev['type']}] {ev['summary']}")
    for c in confirm_pending():
        parts.append(f"[{c['agent']} 待确认] {c['question']}")
    return "\n".join(parts) if parts else ""


def query(agent: str, task: str, timeout_s: int = 120) -> str:
    """执行 agent 任务并返回文本结果(同时记 done 事件)。"""
    res = run_agent(agent, task, timeout_s)
    if res["ok"]:
        out = (res["out"] or "").strip()
        text = out[:2000] if out else f"{agent} 执行成功(无输出)"
    else:
        text = f"{agent} 执行失败(rc={res['rc']}): {(res['err'] or res['out'])[:600]}"
    events_append(agent, "done", text[:300])
    return text
