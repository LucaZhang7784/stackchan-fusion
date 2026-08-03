# -*- coding: utf-8 -*-
"""codex_hook.py — Codex CLI/桌面版 hooks 事件 -> 融合网关

配合 ~/.codex/hooks.json 使用:
  - UserPromptSubmit : codex 收到新任务 -> progress 事件(机器人播报)
  - PermissionRequest: codex 需要审批 -> question 事件(机器人播报"需要确认")
  - Notification     : 进度通知 -> progress
  - Stop / SessionEnd: 任务结束 -> done(机器人播报完成)

从 stdin 读取 codex hook 的 JSON, POST 到网关 /api/agent_event。
本脚本不向 stdout 输出任何内容: codex 的 PermissionRequest 钩子不返回
decision 时保持默认审批流程, 不会被本脚本替用户做决定。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

GATEWAY_URL = "http://127.0.0.1:8010"
AGENT = "codex"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "gateway" / "config.json"
LOG_PATH = Path(__file__).resolve().parent.parent / "gateway" / "state" / "codex_hook.log"
STATE_PATH = Path(__file__).resolve().parent.parent / "gateway" / "state" / "codex_hook.state.json"
GATEWAY_CWD = Path(__file__).resolve().parent.parent / "gateway"


def _load_token() -> str:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception:
        return ""


def _log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    except Exception:
        pass


def _post(etype: str, summary: str, session_id: str) -> None:
    body = json.dumps(
        {"agent": AGENT, "event": etype, "summary": summary, "session_id": session_id},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY_URL + "/api/agent_event", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_load_token()}"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def _clip(text: str, n: int = 300) -> str:
    return " ".join(str(text).split())[:n]


def _first_text(content) -> str:
    """从 codex 的 content 字段(字符串或 [{"type":"text","text":...}] / output_text)取文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                return str(item.get("text", ""))
            if isinstance(item, dict) and item.get("type") in ("output_text", "input_text", "refusal"):
                t = item.get("text") or item.get("refusal") or ""
                if t:
                    return str(t)
    return ""


def _transcript_summary(payload: dict) -> str:
    """取最后一条 assistant 文本作为结果摘要(优先 inline transcript, 其次 transcript_path)。"""
    inline = payload.get("transcript")
    if isinstance(inline, list) and inline:
        for msg in reversed(inline):
            p = msg.get("payload") if isinstance(msg, dict) else None
            if isinstance(p, dict) and p.get("role") == "assistant":
                text = _first_text(p.get("content"))
                if text.strip():
                    return _clip(text)
    tp = payload.get("transcript_path")
    if tp:
        try:
            lines = Path(tp).read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            for line in reversed(lines):
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                p = o.get("payload") if isinstance(o, dict) else None
                if isinstance(p, dict) and p.get("role") == "assistant":
                    text = _first_text(p.get("content"))
                    if text.strip():
                        return _clip(text)
        except Exception:
            pass
    return ""


def _permission_summary(payload: dict) -> str:
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if isinstance(ti, dict):
        for k in ("command", "file_path", "description", "url", "query", "path"):
            v = ti.get(k)
            if v not in (None, ""):
                return _clip(f"{tool}: {v}") if tool else _clip(v)
    if tool:
        return _clip(tool)
    return "需要审批"


def _recently_done(session_id: str, window_s: int = 120) -> bool:
    """Stop 与 SessionEnd 会先后触发; 同一会话 120 秒内只上报一次 done。"""
    if not session_id:
        return False
    import time
    now = time.time()
    seen: dict = {}
    try:
        if STATE_PATH.exists():
            seen = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        seen = {}
    last = seen.get(session_id, 0)
    seen[session_id] = now
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(seen), encoding="utf-8")
    except Exception:
        pass
    return (now - float(last)) < window_s


def _is_gateway_spawn(cwd: str) -> bool:
    """agents_core.query 以 gateway 目录为 workdir 代跑 codex, 并自行记录 done;
    此时跳过 hook 的 done, 避免机器人重复播报。"""
    if not cwd:
        return False
    try:
        return str(Path(cwd).resolve()).casefold() == str(GATEWAY_CWD.resolve()).casefold()
    except Exception:
        return False


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else b""
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception:
        sys.exit(0)
    hook = data.get("hook_event_name", "")
    session_id = str(data.get("session_id") or data.get("listturn_id") or "")
    _log(f"{hook} session={session_id} payload={json.dumps(data, ensure_ascii=False)[:400]}")
    if hook == "PermissionRequest":
        _post("question", _permission_summary(data), session_id)
    elif hook == "UserPromptSubmit":
        prompt = data.get("prompt") or data.get("message") or "收到新任务"
        # 桌面/CLI 会话的每次聊天消息不上报为队列事件(避免机器人念聊天回声),
        # 只记录日志; 任务完成(Stop/SessionEnd)和需要确认(PermissionRequest)仍上报。
        _log(f"UserPromptSubmit suppressed: {session_id} :: {str(prompt)[:80]}")
    elif hook == "Notification":
        _post("progress", _clip(data.get("message") or "进度更新", 200), session_id)
    elif hook in ("Stop", "SessionEnd"):
        if _is_gateway_spawn(str(data.get("cwd", ""))):
            _log(f"skip done: gateway-spawned {session_id}")
        elif not _recently_done(session_id):
            summary = _transcript_summary(data) or "任务已结束"
            _post("done", summary, session_id)
    sys.exit(0)
