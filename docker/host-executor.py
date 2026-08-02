# -*- coding: utf-8 -*-
"""host-executor.py — Windows 宿主执行器(唯一常驻的 Windows 进程)

docker 里的 fusion-gateway / xiaozhi-bridge 无法直接 spawn 本机 CLI
(claude/codex/agy/pi 需要本机文件系统/认证/VS Code), 所以由本进程代为执行。

API:
  GET  /healthz
  POST /exec   {"agent": "...", "task": "...", "timeout": 120, "mode": "exec"|"probe"}
       返回 {"ok": bool, "rc": int, "out": str, "err": str}

监听: 127.0.0.1:8091  (仅本机; docker 内经 host.docker.internal 访问)
认证: Bearer <FUSION_EXECUTOR_TOKEN>
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("FUSION_EXECUTOR_TOKEN", "executor-dev-token")
HOST = os.environ.get("FUSION_EXECUTOR_HOST", "127.0.0.1")
PORT = int(os.environ.get("FUSION_EXECUTOR_PORT", "8091"))

AGENTS = {
    "claude": {"exec_args": ["-p"], "version_args": ["--version"], "workdir": os.path.expanduser("~")},
    "codex": {"exec_args": ["exec", "--skip-git-repo-check", "--sandbox", "workspace-write"],
              "version_args": ["--version"], "workdir": os.getcwd()},
    "agy": {"exec_args": ["--print"], "version_args": ["--version"], "workdir": os.getcwd()},
    "pi": {"exec_args": ["--print", "--no-session", "--no-context-files"],
           "version_args": ["--version"], "workdir": os.path.expanduser("~")},
}


def _no_window() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _resolve(cli: str) -> str:
    return shutil.which(cli) or cli


def _run(argv, timeout, cwd):
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL, creationflags=_no_window(),
        )
        return {"ok": p.returncode == 0, "rc": p.returncode, "out": p.stdout or "", "err": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "out": "", "err": f"exec timeout(>{timeout}s)"}
    except Exception as e:
        return {"ok": False, "rc": -2, "out": "", "err": str(e)}


def exec_agent(name: str, task: str, timeout: int, mode: str) -> dict:
    cfg = AGENTS.get(name)
    if not cfg:
        return {"ok": False, "rc": -3, "out": "", "err": f"unknown agent: {name}"}
    cli = _resolve(name)
    if mode == "probe":
        argv = [cli] + cfg["version_args"]
    else:
        argv = [cli] + cfg["exec_args"] + [task]
    return _run(argv, timeout, cfg["workdir"])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _auth(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self._json(200, {"ok": True, "pid": os.getpid()})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/exec":
            return self._json(404, {"error": "not found"})
        if not self._auth():
            return self._json(401, {"error": "unauthorized"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        except Exception as e:
            return self._json(400, {"error": f"bad json: {e}"})
        name = str(data.get("agent", "")).lower()
        task = str(data.get("task", ""))
        timeout = int(data.get("timeout", 120))
        mode = str(data.get("mode", "exec"))
        result = exec_agent(name, task, min(max(timeout, 5), 600), mode)
        return self._json(200, result)


if __name__ == "__main__":
    print(f"host-executor listening on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
