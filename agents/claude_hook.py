# -*- coding: utf-8 -*-
"""claude_hook.py — Claude Code hooks 事件 -> 融合网关

配合 ~/.claude/settings.json 的 hooks 配置使用(见 install_claude_hooks.ps1):
  - Stop / SessionEnd : 任务结束, 把最后结果摘要上报(机器人可播报)
  - Notification      : 进度通知上报
从 stdin 读取 claude hook 的 JSON, POST 到网关 /api/agent_event。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

GATEWAY_URL = "http://127.0.0.1:8010"
AGENT = "claude"


def _load_token() -> str:
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "gateway" / "config.json"
        return json.loads(cfg_path.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception:
        return ""


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
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _summary_from_transcript(transcript: list) -> str:
    """取最后一条 assistant 文本作为结果摘要。"""
    for msg in reversed(transcript or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()[:400]
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                joined = " ".join(parts).strip()
                if joined:
                    return joined[:400]
    return ""


if __name__ == "__main__":
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        sys.exit(0)
    hook = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    if hook in ("Stop", "SessionEnd"):
        summary = _summary_from_transcript(data.get("transcript", []))
        if not summary:
            summary = "任务已结束(无文本输出)"
        _post("done", summary, session_id)
    elif hook == "Notification":
        msg = data.get("message", "")
        _post("progress", msg[:300], session_id)
    sys.exit(0)
