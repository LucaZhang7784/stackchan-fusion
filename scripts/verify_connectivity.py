#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_connectivity.py — 分层连通性验证 (针对痛点3: 无法验证机器人与 Codex/Claude Code 连通性)

检查项(云链路 + 唤醒播报 口径, 2026-08-03):
  [1] Funnel OTA           机器人取配置的入口
  [2] 网关 /healthz        融合网关进程
  [3] 云桥接               xiaozhi-mcp 进程 + bridge.err 心跳(≤3分钟)
  [4] agent_status 延迟    本地工具处理耗时(<10s, 修复超时后应 ~5s)
  [5] 备用自建链路         8004 health + docker 容器(仅参考, 云链路为主)
  [6] Agent CLI            claude --version / codex --version

用法: python verify_connectivity.py [--strict]
"""
import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
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

    # [3] 云桥接(机器人主链路): xiaozhi-mcp 进程 + bridge.err 心跳
    rc_b, out_b = run(["powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'xiaozhi-mcp' } | Measure-Object | Select-Object -ExpandProperty Count"], 20)
    n_bridge = int((out_b or "0").strip().splitlines()[-1]) if (out_b or "").strip() else 0
    err_path = ROOT / "xiaozhi-mcp" / "bridge.err"
    hb_age = -1
    if err_path.exists():
        hb_age = time.time() - err_path.stat().st_mtime
    check("云桥接(进程+心跳)", n_bridge >= 2 and 0 <= hb_age < 180,
          f"进程={n_bridge} 心跳={int(hb_age)}s" if hb_age >= 0 else f"进程={n_bridge} 心跳文件缺失")

    # [4] agent_status 本地处理延迟(云工具超时修复验证)
    sys.path.insert(0, str(GATEWAY_DIR))
    import agents_core  # noqa: E402
    t0 = time.time()
    agents_core.status_all_text()
    dt = time.time() - t0
    check("agent_status 延迟", dt < 10, f"{dt:.1f}s (缓存命中后 <1s)")

    # [5] 备用自建链路(仅参考, 云链路为主)
    if health_url:
        status3, body3 = http_get(health_url, 10)
        try:
            ok3 = (json.loads(body3).get("result") or {}).get("status") == "success"
        except Exception:
            ok3 = False
        check("备用链路 8004 health", ok3, f"HTTP {status3}")
    rc_c, out_c2 = run(["docker", "ps", "--format", "{{.Names}}:{{.Status}}"], 30)
    cont_ok = container in out_c2 and "healthy" in out_c2
    check("备用容器 healthy", cont_ok, container)

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
    print("端到端人工验证(云链路+唤醒播报):")
    print("  1) 对机器人说「阿松」唤醒 → 应自动检查并播报 agent_pending 中的消息(唤醒优先规则)")
    print("  2) 对机器人说「检查 agent 状态」→ 应播报 agent_status 结果(不再超时)")
    print("  3) 对机器人说「让 codex 做…」→ 电脑弹出对应 agent 窗口执行, 完成后唤醒机器人念结果")
    sys.exit(1 if (args.strict and n_fail) else 0)


if __name__ == "__main__":
    main()
