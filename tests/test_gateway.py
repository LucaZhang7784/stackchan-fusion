#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""融合网关自检: stdio 方式启动网关, 走 JSON-RPC 验证 initialize / tools/list / tools/call(agent_status)。
运行: python tests/test_gateway.py   (无需机器人/服务器在线)
"""
import json
import platform
import subprocess
import sys
import time
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[1] / "gateway" / "fusion_gateway.py"
EXPECTED_TOOLS = {"agent_status", "robot_status", "ws_probe", "codex_query", "claude_query", "robot_say", "robot_pending"}


def send(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()


def read_until(proc, target_id, timeout=20):
    deadline = time.time() + timeout
    seen = []
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        seen.append(line[:160])
        try:
            obj = json.loads(line)
            if obj.get("id") == target_id:
                return obj
        except Exception:
            continue
    raise TimeoutError(f"id={target_id} 无响应; 最近: {seen[-3:]}")


def main():
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if platform.system() == "Windows" else 0
    proc = subprocess.Popen(
        [sys.executable, str(GATEWAY), "--transport", "stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", creationflags=flags,
    )
    try:
        send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "fusion-selftest", "version": "1.0"}}})
        r1 = read_until(proc, 1)
        assert r1.get("result", {}).get("serverInfo", {}).get("name") == "fusion-gateway", r1
        send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        r2 = read_until(proc, 2)
        tools = [t["name"] for t in r2["result"]["tools"]]
        assert EXPECTED_TOOLS.issubset(set(tools)), f"缺少工具: {EXPECTED_TOOLS - set(tools)}"
        send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "agent_status", "arguments": {}}})
        r3 = read_until(proc, 3)
        content = r3["result"]["content"][0]["text"]
        assert "网关状态" in content, content
        print("PASS: initialize / tools/list / agent_status 全部通过")
        print("工具列表:", ", ".join(sorted(tools)))
        print("---- agent_status ----")
        print(content)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())