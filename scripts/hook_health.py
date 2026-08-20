# -*- coding: utf-8 -*-
"""hook_health.py — StackChan hook 配置自检 + 自动修复 + 链路自检 + 机器人告警

防"配置被改坏导致 hook 静默失效"(2026-08-10 Antigravity hooks.json 事故):
  1) Antigravity : ~/.gemini/config/hooks.json  stackchan 段必须为【扁平结构】+ 7 事件
     (Go 语言服务器 language_server.exe / jsonhook.go 只认扁平命令对象,
      嵌套 {"hooks":[...]} 会被拒载: hooks.go:44 "command hook must specify 'command'")
  2) Claude      : ~/.claude/settings.json     4 个 fusion 钩子(claude_hook.py)
  3) Codex       : ~/.codex/hooks.json         5 个事件(codex_hook.py)
  4) hook 脚本存在 + py_compile
  5) 链路自检: 向网关 POST progress 事件(不播报, 只写事件日志)
  6) Antigravity loader 真实状态: 尾部读取 language_server.log, 检测最近是"Loaded"还是"Failed to parse"
  7) Antigravity 钩子心跳(防误杀): 仅当 Antigravity 在运行 且 loader 有近期活动、
     但 antigravity_hook.log 长期无写入时, 才判定"钩子未触发"(隔夜挂机不告警)

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
APPDATA_DIR = Path(os.environ.get("APPDATA", str(HOME / "AppData" / "Roaming")))
ANTIGRAVITY_HOOKS = HOME / ".gemini" / "config" / "hooks.json"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CODEX_HOOKS = HOME / ".codex" / "hooks.json"
ANTIGRAVITY_HOOK_LOG = ROOT / "gateway" / "state" / "antigravity_hook.log"
LANGUAGE_SERVER_LOG = APPDATA_DIR / "Antigravity" / "logs" / "language_server.log"
HEARTBEAT_STALE_H = 6        # antigravity_hook.log 超过 6h 无写入视为"未触发"(可调)
ACTIVITY_WINDOW_MIN = 60     # 近 60 分钟有"真实会话"(brain transcript 更新)才算"有交互"
WATCHER_STATE = ROOT / "gateway" / "state" / "session_watcher.state.json"
WATCHER_STALE_S = 120        # session_watcher 状态文件超过 120s 无更新视为线程已停

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
        # Antigravity 桌面端 Go 语言服务器(language_server.exe / jsonhook.go)要求
        # 扁平命令对象: 条目顶层直接带 command/type/timeout。
        # 嵌套 {"hooks":[...]} 会被拒载: "command hook must specify 'command'"(hooks.go:44)。
        item: dict = _ag_cmd(ev)
        if ev in ("PreToolUse", "PostToolUse"):
            item["matcher"] = ""
        tpl[ev] = [item]
    return tpl


def _normalize_legacy_namespaces(d: dict) -> bool:
    """把非 stackchan 命名空间里旧的嵌套 {"hooks":[...]} 条目扁平化
    (Antigravity Go 加载器要求条目顶层直接带 command/type/timeout, 嵌套会被整文件拒载:
    "invalid hook ... command hook must specify 'command'")。保留其它用户自定义节点。
    返回是否发生了修改。"""
    changed = False
    for ns, section in d.items():
        if ns == "stackchan" or not isinstance(section, dict):
            continue
        for ev, items in list(section.items()):
            if not isinstance(items, list):
                continue
            flat: list = []
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("hooks"), list):
                    flat.extend(h for h in it["hooks"] if isinstance(h, dict))
                    changed = True
                else:
                    flat.append(it)
            section[ev] = flat
    return changed


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
    """自动修复前强备份: .bak-auto-repair-YYYYMMDD-HHMMSS"""
    bak = path.with_name(f"{path.name}.bak-auto-repair-{datetime.now():%Y%m%d-%H%M%S}")
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


def _read_tail(path: Path, n: int = 100) -> list[str]:
    """只读文件末尾(窗口 ≤64KB), 严禁全量扫描几 MB 的大日志。"""
    try:
        size = path.stat().st_size
        if size <= 0:
            return []
        with open(path, "rb") as f:
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", errors="replace")
        return data.splitlines()[-n:]
    except Exception:
        return []


def antigravity_loader_status() -> str:
    """Antigravity Go loader 真实状态: ''=最近成功加载; 'fail'=最近拒载; 'unknown'=无记录。"""
    if not LANGUAGE_SERVER_LOG.exists():
        return "unknown"
    lines = _read_tail(LANGUAGE_SERVER_LOG, 100)
    last_fail, last_loaded = -1, -1
    for i, ln in enumerate(lines):
        if "Failed to parse hooks file" in ln or "invalid hook" in ln:
            last_fail = i
        elif "Loaded hooks.json" in ln:
            last_loaded = i
    if last_loaded >= 0 and last_fail < last_loaded:
        return ""
    if last_fail >= 0 and last_fail > last_loaded:
        return "fail"
    return "unknown"


def _recent_brain_activity(window_s: float) -> tuple[bool, float]:
    """最近一次真实会话 transcript 更新时间(秒前)。
    只有真实对话/任务才会写 brain transcript;
    language_server.log 会被后台 CDP/权限日志持续 touch, 不能当交互信号(2026-08-11 误报教训)。"""
    newest = float("inf")
    for base in (HOME / ".gemini" / "antigravity" / "brain",
                 HOME / ".gemini" / "antigravity-ide" / "brain"):
        if not base.is_dir():
            continue
        for p in base.rglob("transcript_full.jsonl"):
            try:
                age = time.time() - p.stat().st_mtime
                if age < newest:
                    newest = age
            except Exception:
                continue
    return newest <= window_s, newest


def antigravity_heartbeat() -> str:
    """防误杀心跳: Antigravity 在运行 + 最近有真实会话(transcript 更新),
    但 hook 日志长期无写入 -> 告警。挂机隔夜(无会话活动)一律不告警。
    返回 '' 正常, 否则异常描述。"""
    try:
        tl = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Antigravity.exe"],
                            capture_output=True, text=True, timeout=15)
        if "Antigravity.exe" not in tl.stdout:
            return ""  # 进程未运行: 无钩子属正常, 不告警
    except Exception:
        return ""
    now = time.time()
    hook_age = now - ANTIGRAVITY_HOOK_LOG.stat().st_mtime if ANTIGRAVITY_HOOK_LOG.exists() else float("inf")
    if hook_age <= HEARTBEAT_STALE_H * 3600:
        return ""  # 钩子在正常写入
    active, newest_age = _recent_brain_activity(ACTIVITY_WINDOW_MIN * 60)
    if not active:
        return ""  # 无真实会话 -> 挂机, 不告警
    if newest_age < 180:
        return ""  # 会话仍在写入(未结束), 暂不告警
    return (f"钩子未触发({ANTIGRAVITY_HOOK_LOG.name} 已 {hook_age/3600:.1f}h 无写入, "
            f"但 {newest_age/60:.0f} 分钟前有真实会话活动)")


def check_antigravity() -> str:
    """返回 '' 表示正常, 否则返回异常描述; 可修复则就地修复。"""
    if not ANTIGRAVITY_HOOKS.exists():
        return f"缺少 {ANTIGRAVITY_HOOKS}"
    try:
        d = json.loads(ANTIGRAVITY_HOOKS.read_text(encoding="utf-8-sig"))
    except Exception as e:
        return f"Antigravity hooks.json 解析失败: {e}"
    # v08.29: 先扁平化其它命名空间(如 promlight)的旧嵌套结构 —— 嵌套会导致
    # Go 加载器整文件拒载, 只重建 stackchan 段无法消除异常
    if _normalize_legacy_namespaces(d):
        _backup(ANTIGRAVITY_HOOKS)
        try:
            ANTIGRAVITY_HOOKS.write_text(
                json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            log("Antigravity hooks.json 已扁平化修复(旧嵌套结构 -> 平命令对象)")
            return "repaired"
        except Exception as e:
            return f"Antigravity 修复失败: {e}"
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
        # 必须是扁平结构: 条目顶层直接含 command 且指向 antigravity_hook.py
        flat = any(
            isinstance(it, dict) and "antigravity_hook.py" in str(it.get("command", ""))
            for it in items
        )
        if not flat:
            bad.append(ev)
    if not bad:
        return ""
    # 修复: 强备份后仅重建 stackchan 段(增量修复, 保留其它用户自定义节点)
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
        d = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8-sig"))
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
        d = json.loads(CODEX_HOOKS.read_text(encoding="utf-8-sig"))
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


def check_watcher() -> str:
    """session_watcher 兜底监听器保固: 脚本语法 + 网关内 watcher 线程心跳。

    watcher 每 ~5s 写一次状态文件; 超过 WATCHER_STALE_S 无更新说明线程已停,
    Codex 续传会话的 transcript 兜底播报会失效, 必须尽早告警。"""
    ws = ROOT / "scripts" / "session_watcher.py"
    if ws.exists():
        r = subprocess.run([*PY.split(), "-m", "py_compile", str(ws)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return f"session_watcher.py 语法错误: {(r.stderr or r.stdout)[:150]}"
    if not WATCHER_STATE.exists():
        return "session_watcher 状态文件缺失(网关 watcher 线程未运行)"
    age = time.time() - WATCHER_STATE.stat().st_mtime
    if age > WATCHER_STALE_S:
        return f"session_watcher 心跳过期(最后写入 {age:.0f}s 前, watcher 可能已停)"
    return ""


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
    watcher_r = check_watcher()
    if watcher_r:
        issues.append(watcher_r)

    # Antigravity loader 真实状态(尾部读取, 防全量扫描大日志)
    ls = antigravity_loader_status()
    if ls == "fail":
        issues.append("Antigravity: language_server 拒载 hooks.json(Failed to parse, 结构可能被改坏)")
    # Antigravity 钩子心跳(防误杀: 仅当有交互却无写入时告警)
    hb = antigravity_heartbeat()
    if hb:
        issues.append("Antigravity: " + hb)

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
