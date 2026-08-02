# -*- coding: utf-8 -*-
"""claude_run.py — 带机器人确认回环的 Claude Code 无头执行器

用法:
  python claude_run.py "task" [workdir]

行为:
  - 以 --permission-prompt-tool robot-confirm.confirm 运行 claude -p
    (权限确认会走 confirm_mcp.py -> 融合网关 -> 机器人语音确认)
  - 结束后把结果摘要上报为 done 事件(机器人可播报)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIRM_MCP = ROOT / "agents" / "confirm_mcp.py"
GATEWAY_URL = "http://127.0.0.1:8010"
TIMEOUT_SECS = int(os.environ.get("CLAUDE_RUN_TIMEOUT", "600"))


def _load_token() -> str:
    try:
        cfg = json.loads((ROOT / "gateway" / "config.json").read_text(encoding="utf-8"))
        return cfg.get("auth_token", "")
    except Exception:
        return ""


def _post(etype: str, summary: str, session_id: str = "") -> None:
    body = json.dumps(
        {"agent": "claude", "event": etype, "summary": summary, "session_id": session_id},
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


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python claude_run.py \"task\" [workdir]", file=sys.stderr)
        return 2
    task = sys.argv[1]
    workdir = sys.argv[2] if len(sys.argv) > 2 else str(Path.home())

    mcp_cfg = {
        "mcpServers": {
            "robot-confirm": {
                "command": sys.executable,
                "args": [str(CONFIRM_MCP)],
            }
        }
    }
    cfg_path = Path(tempfile.gettempdir()) / "robot_confirm_mcp.json"
    cfg_path.write_text(json.dumps(mcp_cfg, ensure_ascii=False), encoding="utf-8")

    claude = os.environ.get("CLAUDE_BIN", "claude")
    cmd = [
        claude, "-p", task,
        "--mcp-config", str(cfg_path),
        "--permission-prompt-tool", "robot-confirm.confirm",
        "--permission-mode", "acceptEdits",
        "--output-format", "text",
    ]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT_SECS, cwd=workdir,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        _post("error", f"claude 执行超时(>{TIMEOUT_SECS}s)", "")
        print("claude timeout", file=sys.stderr)
        return 1
    except Exception as e:
        _post("error", f"claude 启动失败: {e}", "")
        print(f"claude spawn error: {e}", file=sys.stderr)
        return 1

    out = (p.stdout or "").strip()
    if p.returncode == 0:
        _post("done", out[:400] if out else "claude 执行成功(无输出)", "")
        print(out)
    else:
        err = (p.stderr or "").strip()[:600]
        _post("error", f"claude 退出码 {p.returncode}: {err}", "")
        print(err, file=sys.stderr)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
