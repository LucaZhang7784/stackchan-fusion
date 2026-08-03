# -*- coding: utf-8 -*-
"""confirm_mcp.py — Claude Code permission-prompt-tool 桥接(docker 版)

把 claude 的权限确认请求转给机器人:
  1) 注册待确认问题 -> 网关(排队/推送), 拿到 confirmation_id
  2) HTTP 轮询网关 confirm_status, 等待用户语音回答(agent_confirm 回写)
  3) 把回答映射为 allow/deny 返回给 claude

用法(claude_run.py 内部使用):
  claude -p TASK --mcp-config confirm_mcp.json --permission-prompt-tool robot-confirm.confirm
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

GATEWAY_URL = os.environ.get("FUSION_GATEWAY_URL", "http://127.0.0.1:8010")
AGENT = "claude"
POLL_SECS = 300
POLL_INTERVAL = 2


def _load_token() -> str:
    try:
        from pathlib import Path
        cfg_path = Path(__file__).resolve().parent.parent / "gateway" / "config.json"
        return json.loads(cfg_path.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception as e:
        return os.environ.get("FUSION_GATEWAY_TOKEN", "")


TOKEN = _load_token()


def _post_event(etype: str, summary: str, reply_file: str = "") -> dict:
    body = json.dumps(
        {"agent": AGENT, "event": etype, "summary": summary, "reply_file": reply_file},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY_URL + "/api/agent_event", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": str(e)}


NEGATIVE = ("不行", "拒绝", "不要", "取消", "停", "别", "否", "不", "no", "deny", "abort")
POSITIVE = ("允许", "可以", "好", "同意", "继续", "是", "确认", "执行", "批准", "ok", "yes", "allow", "run")


def _parse_answer(text: str) -> str:
    t = (text or "").strip().lower()
    for w in NEGATIVE:
        if w in t:
            return "deny"
    for w in POSITIVE:
        if w in t:
            return "allow"
    return "deny"  # 不明确 -> 保守拒绝


def _wait_answer(confirmation_id: str) -> str:
    deadline = time.time() + POLL_SECS
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                f"{GATEWAY_URL}/api/agent/confirm_status?id={confirmation_id}",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                st = json.loads(r.read().decode("utf-8", "replace"))
            if st.get("answered"):
                return str(st.get("answer", "")).strip()
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    return ""


from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("robot-confirm")


@mcp.tool()
def confirm(question: str) -> str:
    """处理 claude 的权限确认请求: 把问题发给机器人, 等待用户语音回答, 返回 allow/deny。

    Args:
        question: claude 收到的权限确认请求全文(要执行的操作/要写的文件等)。
    """
    resp = _post_event("question", question, "")
    if resp.get("error"):
        return '{"permission":"deny","reason":"机器人确认桥接不可用: %s"}' % resp["error"]
    cid = str(resp.get("confirmation_id", ""))
    if not cid:
        return '{"permission":"deny","reason":"网关未返回确认ID"}'
    answer = _wait_answer(cid)
    if not answer:
        return '{"permission":"deny","reason":"用户在5分钟内未回应, 已安全拒绝"}'
    decision = _parse_answer(answer)
    if decision == "allow":
        return '{"permission":"allow"}'
    return json.dumps({"permission": "deny", "reason": f"用户拒绝: {answer[:120]}"}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
