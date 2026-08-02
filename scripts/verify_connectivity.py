#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_connectivity.py — 分层连通性验证 (针对痛点3: 无法验证机器人与 Codex/Claude Code 连通性)

检查项:
  [1] Funnel OTA           机器人取配置的入口
  [2] 网关 /healthz        融合网关进程
  [3] MCP接入点 health     8004 端点 (tool/robot 连接数)
  [4] 服务端MCP注册        容器日志里 fusion 工具是否注册
  [5] 机器人在线           容器日志最近 N 分钟是否出现机器人 MAC / 设备工具数
  [6] Agent CLI            claude --version / codex --version

用法: python verify_connectivity.py [--strict]
"""
import argparse
import json
import platform
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DIR = ROOT / "gateway"


def load_cfg():
    p = GATEWAY_DIR / "config.json"
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def http_get(url, timeout=15):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "verify-connectivity/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return None, str(e)


def run(argv, timeout=30):
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if platform.system() == "Windows" else 0
    if argv and argv[0] in ("claude", "codex"):
        exe = shutil.which(argv[0]) or argv[0]
        if exe.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", exe] + [a.replace('"', "'") for a in argv[1:]]
        else:
            argv = [exe] + argv[1:]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=flags)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, str(e)


CHECKS = []


def check(name, ok, detail):
    CHECKS.append((name, ok, detail))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="任一核心项失败则退出码 1")
    args = ap.parse_args()
    cfg = load_cfg()
    ota = cfg.get("ota_url", "https://YOUR_FUNNEL_DOMAIN.ts.net/xiaozhi/ota/")
    mac = cfg.get("robot_mac", "AA:BB:CC:DD:EE:FF")
    container = cfg.get("docker_container", "xiaozhi-esp32-server")
    lookback = cfg.get("docker_log_lookback_minutes", 120)
    health_url = cfg.get("endpoint_health_url", "")

    # [1] OTA
    status, body = http_get(ota, 15)
    check("Funnel OTA", status == 200 and "websocket" in body.lower(), f"HTTP {status}")

    # [2] 网关
    status2, body2 = http_get("http://127.0.0.1:8010/healthz", 5)
    gw_ok = status2 == 200
    check("网关 /healthz", gw_ok, f"HTTP {status2}" + (f" tools={json.loads(body2).get('tools')}" if gw_ok else ""))

    # [3] MCP 接入点
    if health_url:
        status3, body3 = http_get(health_url, 10)
        try:
            data = json.loads(body3)
            result = data.get("result") or {}
            conns = result.get("connections") or {}
            ok3 = result.get("status") == "success"
            check("MCP接入点 health", ok3, f"tool={conns.get('tool_connections') or '-'} robot={conns.get('robot_connections') or '-'} total={conns.get('total_connections') or '-'}")
        except Exception as e:
            check("MCP接入点 health", False, f"解析失败: {e}")
    else:
        check("MCP接入点 health", False, "未配置 endpoint_health_url")

    # [4] 服务端 MCP 注册
    import re
    rc, logs = run(["docker", "logs", "--since", f"{lookback}m", container], 40)
    reg = re.findall(r"服务端MCP客户端已连接，可用工具:\s*(\[[^\]]*\])", logs)
    last_tools = reg[-1] if reg else "[]"
    check("服务端MCP注册", ("fusion" in last_tools.lower() or "codex_query" in last_tools.lower()), last_tools)

    # [5] 机器人在线
    logs_l = logs.lower()
    mac_plain = mac.lower().replace(":", "")
    seen = mac_plain in logs_l or mac.lower() in logs_l
    dev_tools = re.findall(r"客户端设备支持的工具数量:\s*(\d+)", logs)
    check("机器人在线", seen, f"设备工具数={dev_tools[-1] if dev_tools else 'N/A'}")

    # [6] Agent CLI
    _, out_c = run([cfg.get("claude_cli", "claude"), "--version"], 20)
    claude_ok = "Claude" in out_c or "claude" in out_c.lower()
    check("Claude CLI", claude_ok, out_c.strip()[:100])
    rc_x, out_x = run([cfg.get("codex_cli", "codex"), "--version"], 20)
    check("Codex CLI", rc_x == 0, out_x.strip()[:100] if rc_x == 0 else f"不可用(商店版常见, 不影响融合): {out_x.strip()[:80]}")

    # 输出
    print("\n" + "=" * 70)
    print("StackChan 融合链路连通性验证")
    print("=" * 70)
    n_fail = 0
    for name, ok, detail in CHECKS:
        tag = "PASS" if ok else ("SKIP" if detail.startswith("不可用") and name == "Codex CLI" else "FAIL")
        if tag == "FAIL":
            n_fail += 1
        print(f"[{tag}] {name:20s} {detail}")
    print("=" * 70)
    print("端到端人工验证: 对机器人说「查一下Codex/Claude的状态」, 应听到机器人播报 agent_status 的结果。")
    print("Codex/Claude侧验证: claude -p \"调用 robot_status\" 或在本机 MCP 客户端里调 robot_say(\"测试消息\") 再对机器人说「有什么消息」。")
    sys.exit(1 if (args.strict and n_fail) else 0)


if __name__ == "__main__":
    main()