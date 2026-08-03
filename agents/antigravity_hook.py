# -*- coding: utf-8 -*-
"""antigravity_hook.py - Antigravity 桌面版 / agy CLI hooks 事件 -> 融合网关

配合 ~/.gemini/config/hooks.json 的 "fusion" 段使用:
  - PreToolUse     : 权限敏感工具执行前, 上报 question 事件(机器人唤醒后播报"需要确认")
  - PermissionRequest / PermissionDenied / Elicitation: 若触发则上报 question
  - Stop           : 任务结束, 上报 done(机器人可播报完成)
  桌面版会话归属 agent=antigravity; agy CLI 会话(artifactDirectoryPath 含
  "antigravity-cli")归属 agent=agy, 与网关 AGENT_CLIS 对齐。

从 stdin 读取 Antigravity 语言服务器的 hook JSON(protojson, camelCase),
POST 到网关 /api/agent_event。无论成败都必须按协议向 stdout 回写决策,
绝不能阻断/挂起 agent。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

GATEWAY_URL = "http://127.0.0.1:8010"
AGENT = "antigravity"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "gateway" / "config.json"
LOG_PATH = Path(__file__).resolve().parent.parent / "gateway" / "state" / "antigravity_hook.log"

# 这类工具在 ASK 策略下通常会弹出确认(文件写/命令执行/联网/MCP)
QUESTION_TOOLS = {
    "run_command", "write_file", "apply_patch", "edit_file", "batch_write",
    "replace_file_content", "insert_text", "delete_text", "web_fetch",
    "web_search", "internet", "mcp_tool", "browser_open", "browser_click",
    "browser_type", "browser_execute",
}


def _load_token() -> str:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception:
        return ""


def _post_agent(agent: str, etype: str, summary: str, session_id: str) -> None:
    body = json.dumps(
        {"agent": agent, "event": etype, "summary": summary, "session_id": session_id},
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


def _log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    except Exception:
        pass


def _emit(obj: dict) -> None:
    try:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()
    except Exception:
        pass


def _tool_name(payload: dict) -> str:
    tc = payload.get("toolCall") or {}
    return str(tc.get("name") or payload.get("tool_name") or "").lower()


def _tool_detail(payload: dict) -> str:
    tc = payload.get("toolCall") or {}
    args = tc.get("args") or payload.get("tool_input") or {}
    if isinstance(args, dict):
        for k in ("CommandLine", "command", "file_path", "AbsolutePath", "DirectoryPath",
                  "url", "query", "pattern", "description", "path"):
            v = args.get(k)
            if v not in (None, ""):
                return " ".join(str(v).split())[:200]
    return ""


def _question_summary(payload: dict) -> str:
    tool = _tool_name(payload)
    detail = _tool_detail(payload)
    if tool:
        return f"{tool}: {detail}".rstrip(": ") if detail else tool
    msg = payload.get("message") or payload.get("question") or payload.get("prompt") or ""
    if msg:
        return " ".join(str(msg).split())[:200]
    return "需要确认"


def _transcript_summary(payload: dict) -> str:
    """从 transcriptPath 尾部找最后一条 PLANNER_RESPONSE 文本作为任务结果摘要。"""
    tp = payload.get("transcriptPath") or payload.get("artifactDirectoryPath")
    if not tp:
        return ""
    try:
        lines = Path(tp).read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        for line in reversed(lines):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if str(o.get("type", "")).upper() == "PLANNER_RESPONSE":
                content = o.get("content")
                if isinstance(content, str) and content.strip():
                    return " ".join(content.split())[:300]
    except Exception:
        pass
    return ""


def _parse_args(argv):
    """抽 --agent 值, 首个位置参数作为事件名(与 PromLight 约定一致)。"""
    agent, event = "", ""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--agent":
            i += 1
            if i < len(argv):
                agent = argv[i]
            i += 1
            continue
        if a.startswith("--agent="):
            agent = a.split("=", 1)[1]
            i += 1
            continue
        if not event:
            event = a
        i += 1
    return agent, event


def main(argv) -> int:
    try:
        raw = sys.stdin.buffer.read() if sys.stdin else b""
    except Exception:
        raw = b""
    payload = {}
    if raw and raw.strip():
        try:
            payload = json.loads(raw.decode("utf-8-sig", "replace"))
        except Exception:
            payload = {}

    _agent, event = _parse_args(argv)
    if not event:
        event = str(payload.get("hook_event_name", ""))
    session_id = str(payload.get("conversationId") or payload.get("session_id") or "")
    agent = "agy" if "antigravity-cli" in str(payload.get("artifactDirectoryPath", "")) else AGENT
    _log(f"{event} {json.dumps(payload, ensure_ascii=False)[:300]}")

    if event == "PreToolUse":
        tool = _tool_name(payload)
        if tool in QUESTION_TOOLS or tool.startswith("mcp_") or tool.startswith("browser_"):
            _post_agent(agent, "question", _question_summary(payload), session_id)
        _emit({"decision": "allow"})
    elif event in ("PermissionRequest", "PermissionDenied", "Elicitation"):
        _post_agent(agent, "question", _question_summary(payload), session_id)
        _emit({"decision": "allow"})
    elif event == "Stop":
        summary = _transcript_summary(payload) or str(payload.get("terminationReason") or "任务已结束")
        _post_agent(agent, "done", summary, session_id)
        _emit({"decision": "allow"})
    elif event == "PostToolUse":
        _emit({})
    else:
        _emit({"decision": "allow"})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:
        _emit({"decision": "allow"})
        sys.exit(0)
