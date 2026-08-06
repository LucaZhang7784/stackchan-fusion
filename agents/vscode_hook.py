# -*- coding: utf-8 -*-
"""vscode_hook.py — VS Code Task/Terminal 完成事件 -> 融合网关

用法(在 VS Code tasks.json 的任务末尾追加一条, 或终端命令末尾调用):
  python vscode_hook.py "VS Code 任务已完成"
  python vscode_hook.py --summary "自定义摘要"
  echo '{"summary":"..."}' | python vscode_hook.py --stdin

上报到网关 /api/agent_event: {"agent": "vscode", "event": "done", "summary": ...},
由网关写入 pending 队列并立即推送给机器人播报。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

GATEWAY_URL = "http://127.0.0.1:8010"
AGENT = "vscode"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "gateway" / "config.json"
LOG_PATH = Path(__file__).resolve().parent.parent / "gateway" / "state" / "vscode_hook.log"


def _load_token() -> str:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception as e:
        _log(f"[ERROR] config.json 解析失败(token 为空): {e}")
        return ""


def _log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    except Exception:
        pass


def _post_done(summary: str) -> None:
    body = json.dumps(
        {"agent": AGENT, "event": "done", "summary": summary, "session_id": "vscode-task"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY_URL + "/api/agent_event", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_load_token()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            _log(f"posted done ({r.status}): {summary[:120]}")
    except Exception as e:
        _log(f"post failed: {e} :: {summary[:120]}")


def main() -> int:
    args = sys.argv[1:]
    summary = ""
    if "--stdin" in args:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace") if hasattr(sys.stdin, "buffer") else ""
        try:
            summary = str(json.loads(raw or "{}").get("summary") or "").strip()
        except Exception:
            summary = " ".join(raw.split())
    if not summary:
        for i, a in enumerate(args):
            if a == "--summary" and i + 1 < len(args):
                summary = args[i + 1]
                break
    if not summary:
        # 首个位置参数作为摘要(兼容 tasks.json 直接传文本)
        for a in args:
            if not a.startswith("-"):
                summary = a
                break
    summary = (summary or "VS Code 任务已完成").strip()[:500]
    _post_done(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
