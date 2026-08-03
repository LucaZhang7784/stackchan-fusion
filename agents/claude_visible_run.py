# -*- coding: utf-8 -*-
"""claude_visible_run.py — 在可见窗口运行 claude -p 并主动上报 done 到网关。

claude -p (print 模式) 不触发 Claude Code hooks, 机器人收不到完成事件;
本脚本捕获 claude 输出, 完成后把摘要 POST 到融合网关 /api/agent_event。
任务经环境变量 ASON_TASK 传入, 工作目录 = 窗口脚本的 cd(项目目录)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # fusion.firmware.0731
CFG_PATH = ROOT / "gateway" / "config.json"
GATEWAY_URL = "http://127.0.0.1:8010/api/agent_event"
OUTBOX_DIR = ROOT / "xiaozhi-mcp" / "outbox"


def _post(etype: str, summary: str) -> None:
    try:
        token = json.loads(CFG_PATH.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception:
        token = ""
    body = json.dumps(
        {"agent": "claude", "event": etype, "summary": (summary or "")[:500], "session_id": "visible"},
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            GATEWAY_URL, data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


def _outbox_write(task: str, ok: bool, output: str) -> None:
    """同时写 xiaozhi-mcp outbox, 让 agent_result_check 也能取到结果。"""
    try:
        OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
        f = OUTBOX_DIR / f"{int(__import__('time').time() * 1000)}.txt"
        f.write_text(f"[claude] {'OK' if ok else 'FAIL'}: {task}\n\n{output}", encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    task = os.environ.get("ASON_TASK", "") or (sys.argv[1] if len(sys.argv) > 1 else "")
    try:
        cli = shutil.which("claude") or "claude"
        if cli.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c", cli, "-p", task]
        else:
            cmd = [cli, "-p", task]
        r = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1800,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        summary = out[-500:] if out else (err[-500:] or "任务完成(无文本输出)")
        _post("done", summary)
        _outbox_write(task, True, out or err)
        if out:
            sys.stdout.write(out + "\n")
        if err:
            sys.stdout.write("\n[stderr]\n" + err[-500:] + "\n")
    except subprocess.TimeoutExpired:
        _post("error", "claude 执行超时(>30分钟)")
        sys.stdout.write("\n[claude] 执行超时(>30分钟)\n")
    except Exception as e:
        _post("error", f"claude 执行失败: {e}")
        sys.stdout.write(f"\n[claude] 执行失败: {e}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
