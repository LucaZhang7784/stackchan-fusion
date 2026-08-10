# -*- coding: utf-8 -*-
"""hook_health.py — StackChan hook 配置自检 + 自动修复 + 链路自检 + 机器人告警

防"配置被拍平/改坏导致 hook 静默失效"(2026-08-10 Antigravity hooks.json 扁平化事故):
  1) Antigravity : ~/.gemini/config/hooks.json  stackchan 段必须为嵌套结构 + 7 事件
  2) Claude      : ~/.claude/settings.json     4 个 fusion 钩子(claude_hook.py)
  3) Codex       : ~/.codex/hooks.json         5 个事件(codex_hook.py)
  4) hook 脚本存在 + py_compile
  5) 链路自检: 向网关 POST progress 事件(不播报, 只写事件日志)

用法:
  py hook_health.py                  # 检查+修复+链路自检, 打印摘要
  py hook_health.py --alert          # 同上, 且异常/已修复时向机器人推送告警(agent=system)
  py hook_health.py --check-only     # 只检查+修复, 不做链路自检
退出码: 0=全部正常  1=已自动修复  2=存在未修复异常
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENTS = ROOT / "agents"
PY = "C:/WINDOWS/py.EXE -3"
GATEWAY_URL = "http://127.0.0.1:8010"
GATEWAY_CFG = ROOT / "gateway" / "config.json"
LOG_PATH = ROOT / "gateway" / "state" / "hook_health.log"
LAST_RESULT = ROOT / "gateway" / "state" / "hook_health.last.txt"

HOME = Path.home()
ANTIGRAVITY_HOOKS = HOME / ".gemini" / "config" / "hooks.json"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CODEX_HOOKS = HOME / ".codex" / "hooks.json"

AGENTS_LIST = ["antigravity", "codex", "claude", "vscode"]
HOOK_SCRIPTS = ["antigravity_hook.py", "codex_hook.py", "claude_hook.py", "vscode_hook.py"]

# Antigravity stackchan 段标准模板(嵌套结构 —— 与 8/7 可用版一致; 扁平结构会被加载器忽略)
def _ag_cmd(event: str) -> dict:
    return {
        "type": "command",
        "command": f"{PY} {AGENTS.as_posix()}/antigravity_hook.py --agent antigravity {event}",
        "timeout": 10,
    }


def antigravity_template() -> dict:
    tpl: dict = {"enabled": True}
    for ev in ("SessionStart", "PreToolUse", "PostToolUse", "PermissionRequest",
               "PermissionDenied", "Elicitation", "Stop"):
        item: dict = {"hooks": [_ag_cmd(ev)]}
        if ev in ("PreToolUse", "PostToolUse"):
            item["matcher"] = ""
        tpl[ev] = [item]
    return tpl


CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "PermissionRequest", "Stop", "SessionEnd"]
CLAUDE_EVENTS = ["Stop", "SessionEnd", "Notification", "PermissionRequest"]


def log(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
    except Exception:
        pass


def _load_token() -> str:
    try:
        return json.loads(GATEWAY_CFG.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception:
        return ""


def _backup(path: Path) -> None:
    bak = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}-health")
    try:
        bak.write_bytes(path.read_bytes())
        log(f"备份 {path.name} -> {bak.name}")
    except Exception as e:
        log(f"备份失败 {path.name}: {e}")


def _post(url: str, body: dict, timeout: float = 5.0) -> bool:
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_load_token()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


def _gateway_ok() -> bool:
    """GET /healthz 探测网关(注意: /healthz 只接受 GET)。"""
    try:
        with urllib.request.urlopen(GATEWAY_URL + "/healthz", timeout=3.0) as r:
            return r.status == 200
    except Exception:
        return False


def check_antigravity() -> str:
    """返回 '' 表示正常, 否则返回异常描述; 可修复则就地修复。"""
    if not ANTIGRAVITY_HOOKS.exists():
        return f"缺少 {ANTIGRAVITY_HOOKS}"
    try:
        d = json.loads(ANTIGRAVITY_HOOKS.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Antigravity hooks.json 解析失败: {e}"
    sc = d.get("stackchan")
    if not isinstance(sc, dict) or not sc.get("enabled"):
        return "Antigravity stackchan 段缺失或未启用"
    bad = []
    for ev in ("SessionStart", "PreToolUse", "PostToolUse", "PermissionRequest",
               "PermissionDenied", "Elicitation", "Stop"):
        items = sc.get(ev)
        if not isinstance(items, list) or not items:
            bad.append(ev)
            continue
        # 必须嵌套结构: 组内含 "hooks" 数组, 命令指向 antigravity_hook.py
        nested = any(
            isinstance(it, dict) and isinstance(it.get("hooks"), list)
            and any("antigravity_hook.py" in str(h.get("command", "")) for h in it["hooks"])
            for it in items
        )
        if not nested:
            bad.append(ev)
    if not bad:
        return ""
    # 修复: 备份后重建 stackchan 段(保留 promlight 等其它段)
    _backup(ANTIGRAVITY_HOOKS)
    d["stackchan"] = antigravity_template()
    try:
        ANTIGRAVITY_HOOKS.write_text(
            json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"Antigravity stackchan 段已重建(缺失/损坏事件: {bad})")
        return "repaired"
    except Exception as e:
        return f"Antigravity 修复失败: {e}"


def check_claude() -> str:
    if not CLAUDE_SETTINGS.exists():
        return f"缺少 {CLAUDE_SETTINGS}"
    try:
        d = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Claude settings.json 解析失败: {e}"
    hooks = d.get("hooks") or {}
    missing = []
    for ev in CLAUDE_EVENTS:
        items = hooks.get(ev)
        ok = isinstance(items, list) and any(
            isinstance(it, dict) and isinstance(it.get("hooks"), list)
            and any("claude_hook.py" in str(h.get("command", "")) for h in it["hooks"])
            for it in items
        )
        if not ok:
            missing.append(ev)
    if not missing:
        return ""
    # 修复: 重跑官方安装脚本(幂等合并)
    inst = AGENTS / "install_claude_hooks.ps1"
    if not inst.exists():
        return f"Claude hooks 缺失({missing})且无安装脚本 {inst}"
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(inst)],
            capture_output=True, text=True, timeout=60,
        )
        ok = r.returncode == 0 and check_claude() == ""
        if ok:
            log(f"Claude hooks 已重装修复(缺失事件: {missing})")
            return "repaired"
        return f"Claude hooks 缺失({missing})且重装后仍异常: {r.stderr[:200] or r.stdout[:200]}"
    except Exception as e:
        return f"Claude hooks 重装失败: {e}"


def check_codex() -> str:
    if not CODEX_HOOKS.exists():
        return f"缺少 {CODEX_HOOKS}"
    try:
        d = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Codex hooks.json 解析失败: {e}"
    hooks = d.get("hooks") or {}
    missing = []
    for ev in CODEX_EVENTS:
        items = hooks.get(ev)
        ok = isinstance(items, list) and any(
            isinstance(it, dict) and isinstance(it.get("hooks"), list)
            and any("codex_hook.py" in str(h.get("command", "")) for h in it["hooks"])
            for it in items
        )
        if not ok:
            missing.append(ev)
    if not missing:
        return ""
    # 修复: 备份后为缺失事件插入 fusion 钩子(保留其它现有条目)
    _backup(CODEX_HOOKS)
    for ev in missing:
        entry = {"hooks": [{
            "type": "command",
            "command": f"{PY} {AGENTS.as_posix()}/codex_hook.py",
            "statusMessage": "Notifying StackChan",
            "timeout": 10,
        }]}
        hooks.setdefault(ev, []).insert(0, entry)
    d["hooks"] = hooks
    try:
        CODEX_HOOKS.write_text(
            json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"Codex hooks 已补齐(缺失事件: {missing})")
        return "repaired"
    except Exception as e:
        return f"Codex hooks 修复失败: {e}"


def check_scripts() -> list[str]:
    issues = []
    for s in HOOK_SCRIPTS:
        p = AGENTS / s
        if not p.exists():
            issues.append(f"脚本缺失 {p}")
            continue
        r = subprocess.run([*PY.split(), "-m", "py_compile", str(p)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            issues.append(f"{s} 语法错误: {(r.stderr or r.stdout)[:150]}")
    return issues


def selftest() -> tuple[int, str]:
    """向网关 POST progress(不播报)验证 hook->网关 通路; 返回 (成功数, 摘要)。"""
    if not _gateway_ok():
        return -1, "网关离线, 跳过链路自检"
    ok = 0
    for agent in AGENTS_LIST:
        body = {
            "agent": agent, "event": "progress",
            "summary": "hook_health selftest",
            "session_id": "hookhealth",
            "msg_uid": f"hookselftest-{agent}-{int(time.time())}",
        }
        if _post(GATEWAY_URL + "/api/agent_event", body):
            ok += 1
    return ok, f"链路自检 {ok}/{len(AGENTS_LIST)} 通过"


def main() -> int:
    args = sys.argv[1:]
    alert = "--alert" in args
    check_only = "--check-only" in args
    issues: list[str] = []
    repaired: list[str] = []

    for name, fn in (("Antigravity", check_antigravity),
                     ("Claude", check_claude),
                     ("Codex", check_codex)):
        try:
            r = fn()
        except Exception as e:
            r = f"{name} 检查异常: {e}"
        if not r:
            continue
        if r == "repaired":
            repaired.append(name)
        else:
            issues.append(f"{name}: {r}")

    script_issues = check_scripts()
    issues.extend(script_issues)

    st_note = ""
    if not check_only:
        ok_n, st_note = selftest()
        if ok_n == 0 and ok_n != -1:
            issues.append(f"链路自检全部失败({st_note})")

    # 状态: 有未修复异常 -> 2; 仅已修复 -> 1; 干净 -> 0
    if issues:
        status = 2
    elif repaired:
        status = 1
    else:
        status = 0

    lines = [f"Hook 自检结果: {'全部正常' if status == 0 else ('已自动修复' if status == 1 else '存在异常')}"]
    if repaired:
        lines.append("已修复: " + ", ".join(repaired))
    if issues:
        lines.append("异常: " + "; ".join(issues))
    if st_note:
        lines.append(st_note)
    summary = "\n".join(lines)
    log(summary)
    try:
        LAST_RESULT.write_text(summary + "\n", encoding="utf-8")
    except Exception:
        pass
    print(summary)

    if alert and status != 0:
        kind = "已自动修复" if status == 1 else "存在异常"
        detail = "; ".join(repaired + issues)[:200]
        alert_body = {
            "agent": "system", "event": "done",
            "summary": f"系统自检：{kind} - {detail}",
            "session_id": "hookhealth",
            "msg_uid": f"syscheck-{datetime.now():%Y%m%d-%H}-{kind[:2]}",
        }
        if _post(GATEWAY_URL + "/api/agent_event", alert_body):
            log(f"已推送机器人告警: {alert_body['summary'][:120]}")
            print("已推送机器人告警(去重: " + alert_body["msg_uid"] + ")")
    return status


if __name__ == "__main__":
    sys.exit(main())
