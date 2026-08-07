# -*- coding: utf-8 -*-
"""claude_hook.py — Claude Code hooks 事件 -> 融合网关

配合 ~/.claude/settings.json 的 hooks 配置使用(见 install_claude_hooks.ps1):
  - Stop / SessionEnd : 任务结束, 把最后结果摘要上报(机器人可播报)
  - Notification      : 进度通知上报
从 stdin 读取 claude hook 的 JSON, POST 到网关 /api/agent_event。
"""
from __future__ import annotations

import json
import hashlib
import sys
import time
import urllib.request
from pathlib import Path

GATEWAY_URL = "http://127.0.0.1:8010"
AGENT = "claude"
STATE_PATH = Path(__file__).resolve().parent.parent / "gateway" / "state" / "claude_hook.state.json"
LOG_PATH = Path(__file__).resolve().parent.parent / "gateway" / "state" / "claude_hook.log"


def _log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    except Exception:
        pass


def _load_token() -> str:
    try:
        cfg_path = Path(__file__).resolve().parent.parent / "gateway" / "config.json"
        return json.loads(cfg_path.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception as e:
        _log(f"[ERROR] config.json 解析失败(token 为空): {e}")
        return ""


def _post(etype: str, summary: str, session_id: str, msg_uid: str = "") -> None:
    body = json.dumps(
        {"agent": AGENT, "event": etype, "summary": summary, "session_id": session_id, "msg_uid": msg_uid},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY_URL + "/api/agent_event", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_load_token()}"},
    )
    try:
        urllib.request.urlopen(req, timeout=2.5).read()
        _log(f"posted {etype} ok {msg_uid}: {summary[:80]}")
    except Exception as e:
        _log(f"post {etype} FAILED {msg_uid}: {e} :: {summary[:80]}")


def _summary_from_transcript(transcript: list) -> str:
    """取最后一条 assistant 文本作为结果摘要(兼容 str / [text|output_text] / 嵌套)。"""
    for msg in reversed(transcript or []):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                return content.strip()[:400]
            if isinstance(content, list):
                parts = []
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") in ("text", "output_text", "input_text"):
                        t = str(p.get("text") or "").strip()
                        if t:
                            parts.append(t)
                joined = " ".join(parts).strip()
                if joined:
                    return joined[:400]
    return ""


def _summary_from_transcript_path(tp) -> str:
    """Claude Code 会话 JSONL 兜底: 每行 {type, message:{role, content}} 或 {type:assistant, message:{content}},
    取最后一条 assistant 的 text 内容(含中断流的部分响应)。"""
    if not tp:
        return ""
    try:
        lines = Path(tp).read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
        for line in reversed(lines):
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            msg = o.get("message")
            role = o.get("type")
            if isinstance(msg, dict):
                role = msg.get("role") or role
                content = msg.get("content")
            else:
                content = None
            if role != "assistant":
                continue
            if isinstance(content, str) and content.strip():
                return " ".join(content.split())[:400]
            if isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        t = str(p.get("text") or "").strip()
                        if t:
                            parts.append(t)
                joined = " ".join(parts).strip()
                if joined:
                    return joined[:400]
    except Exception:
        pass
    return ""


def _msg_uid(data: dict) -> str:
    """按 (session_id, 最后一条 assistant 消息标识) 归一化 msg_uid:
    Stop 与 SessionEnd 同轮同 uid(合并去重), 不同轮不同 uid。"""
    session = str(data.get("session_id") or "")
    key = ""
    transcript = data.get("transcript")
    if isinstance(transcript, list):
        for msg in reversed(transcript):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content")
                key = str(msg.get("uuid") or json.dumps(content, ensure_ascii=False))
                break
    if not key:
        key = str(data.get("turn_id") or session or "none")
    h = hashlib.sha256(f"{session}|{key}".encode("utf-8")).hexdigest()[:12]
    return f"claude_{session[:8]}_{h}"


def _recently_done(msg_uid: str) -> bool:
    """仅按 msg_uid 去重: 同 msg_uid 的 Stop/SessionEnd 合并; 不同 msg_uid 永不跳过。
    状态表清理 >10min 旧条目(存储卫生), 不设滑动时间窗口。"""
    if not msg_uid:
        return False
    now = time.time()
    seen: dict = {}
    try:
        if STATE_PATH.exists():
            seen = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        seen = {}
    seen = {k: v for k, v in seen.items() if now - float(v) < 600}
    if msg_uid in seen:
        return True
    seen[msg_uid] = now
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(seen), encoding="utf-8")
    except Exception:
        pass
    return False


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else b""
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception:
        sys.exit(0)
    hook = data.get("hook_event_name", "")
    session_id = data.get("session_id", "")
    _log(f"{hook} session={session_id} payload={json.dumps(data, ensure_ascii=False)[:400]}")
    if hook in ("Stop", "SessionEnd"):
        msg_uid = _msg_uid(data)
        if not _recently_done(msg_uid):
            summary = _summary_from_transcript(data.get("transcript", [])) or _summary_from_transcript_path(data.get("transcript_path") or data.get("transcriptPath") or "")
            if not summary:
                summary = "Claude 会话结束(响应可能中断, 详见电脑)"
            _post("done", summary, session_id, msg_uid)
    elif hook == "Notification":
        msg = data.get("message", "")
        _post("progress", msg[:300], session_id)
    elif hook == "PermissionRequest":
        # 交互式 Claude Code 权限弹窗: 上报 question, 机器人主动播报「claude 需要确认: ...」。
        # 不返回 decision, 保持 Claude 默认审批流程(用户仍在终端回答)。
        pr = data.get("permission_request") or {}
        if not isinstance(pr, dict):
            pr = {}
        tool = str(pr.get("tool_name") or data.get("tool_name") or pr.get("action") or "")
        ti = pr.get("tool_input") or data.get("tool_input") or {}
        detail = ""
        if isinstance(ti, dict):
            for k in ("command", "file_path", "path", "url", "description", "query", "pattern", "prompt"):
                v = ti.get(k)
                if v not in (None, ""):
                    detail = " ".join(str(v).split())[:200]
                    break
        summary = f"{tool}: {detail}".rstrip(": ") if detail else (tool or "需要确认")
        _post("question", summary, session_id)
    sys.exit(0)
