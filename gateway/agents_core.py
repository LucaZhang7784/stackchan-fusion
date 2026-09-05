# -*- coding: utf-8 -*-
"""agents_core.py — 多 agent 管理核心(agy/pi/claude/codex), 供 fusion_gateway 与 xiaozhi-mcp 共用。

提供:
  - agent_status(name) : CLI 可用性 + 运行进程 + 待确认数 + 最近事件
  - agent_query(name, task, timeout) : 在 agent 自己的可见窗口执行(结果经 hooks 回流)
  - agent_pending(clear) : 待播报事件 + 待确认问题(机器人/LLM 读取)
  - agent_confirm(name, answer) : 把语音回答写回等待中的 agent
  - event_post(...) / confirm_register / confirm_answer : 内部接口
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

EVENTS_FILE = DATA_DIR / "agent_events.jsonl"
CONFIRM_FILE = DATA_DIR / "agent_confirmations.json"

# docker 方案: 宿主执行器(执行本机 CLI)。连不上时回退本地 spawn, 保证过渡期兼容。
EXECUTOR_URL = os.environ.get("FUSION_EXECUTOR_URL", "http://127.0.0.1:8091")
EXECUTOR_TOKEN = os.environ.get("FUSION_EXECUTOR_TOKEN", "")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _no_window() -> int:
    if os.name == "nt":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _resolve(cli: str) -> str:
    return shutil.which(cli) or cli


def _run(argv: list[str], timeout: int, cwd: str) -> dict:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=cwd, stdin=subprocess.DEVNULL,
            creationflags=_no_window(),
        )
        return {"ok": p.returncode == 0, "rc": p.returncode, "out": p.stdout or "", "err": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "out": "", "err": f"exec timeout(>{timeout}s)"}
    except Exception as e:
        return {"ok": False, "rc": -2, "out": "", "err": str(e)}


def _exec_via_http(name: str, mode: str, task: str, timeout: int) -> dict | None:
    """调宿主执行器; 失败(网络/未部署)返回 None 由调用方回退本地。"""
    try:
        body = json.dumps({"agent": name, "mode": mode, "task": task, "timeout": timeout}).encode("utf-8")
        req = urllib.request.Request(
            EXECUTOR_URL + "/exec", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {EXECUTOR_TOKEN}"},
        )
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def run_agent(name: str, task: str, timeout: int = 120, workdir: str | None = None) -> dict:
    """运行指定 agent 的无头模式。返回 {ok, out, err, rc}。"""
    via_http = _exec_via_http(name, "exec", task, timeout)
    if via_http is not None:
        return via_http
    # 回退: 本地直接 spawn
    cfg = AGENT_CLIS.get(name, {})
    cli = cfg.get("cli", name)
    cli_path = _resolve(cli)  # Windows 上解析到 .CMD 绝对路径, 避免 cmd 歧义
    args = cfg.get("exec_args", [])
    full = [cli_path] + [str(a) for a in args] + [task]
    return _run(full, timeout, workdir or cfg.get("workdir") or str(ROOT))


_probe_cache: dict = {}
_PROBE_TTL = 300  # 秒; CLI 版本短时间内不变, 缓存避免 agent_status 每次启动 CLI


def probe(name: str) -> tuple[bool, str]:
    """探测 agent CLI 是否可用(返回 可用性, 版本首行)。带 300s 缓存 + 进程快检。"""
    now = time.time()
    cached = _probe_cache.get(name)
    if cached and now - cached[0] < _PROBE_TTL:
        return cached[1], cached[2]
    # 快检: 有运行进程直接视为可用(避免每次冷启动 CLI, 这是 agent_status 慢的主因)
    if running_processes(name):
        _probe_cache[name] = (now, True, "进程运行中")
        return True, "进程运行中"
    via_http = _exec_via_http(name, "probe", "", 8)
    if via_http is not None:
        if via_http.get("ok"):
            first = (via_http.get("out") or "").strip().splitlines()
            info = (first[0][:80] if first else "ok")
            _probe_cache[name] = (now, True, info)
            return True, info
        info = (via_http.get("err") or via_http.get("out") or "spawn failed").strip()[:150]
        _probe_cache[name] = (now, False, info)
        return False, info
    cfg = AGENT_CLIS.get(name, {})
    version_args = cfg.get("version_args", ["--version"])
    cli = cfg.get("cli", name)
    cli_path = _resolve(cli)
    full = [cli_path] + version_args
    r = _run(full, 8, cfg.get("workdir") or str(ROOT))
    if r["ok"]:
        first = (r["out"] or "").strip().splitlines()
        info = (first[0][:80] if first else "ok")
        _probe_cache[name] = (now, True, info)
        return True, info
    info = (r["err"] or r["out"] or "spawn failed").strip()[:150]
    _probe_cache[name] = (now, False, info)
    return False, info


def running_processes(name: str) -> list[int]:
    """返回名字匹配的进程 PID(粗略: 按可执行名)。"""
    pids: list[int] = []
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-CimInstance Win32_Process -Filter \"Name='{name}.exe'\" | Select-Object -ExpandProperty ProcessId"],
                capture_output=True, text=True, timeout=10, creationflags=_no_window(),
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.append(int(line))
    except Exception:
        pass
    return pids


# ---------------------------------------------------------------- 事件/确认 存储
def events_append(agent: str, etype: str, summary: str, session_id: str = "") -> dict:
    ev = {"id": uuid.uuid4().hex[:8], "agent": agent, "type": etype,
          "summary": (summary or "")[:500], "session_id": session_id, "ts": _now()}
    with open(EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return ev


def events_read(clear: bool = False) -> list[dict]:
    if not EVENTS_FILE.exists():
        return []
    lines = [l for l in EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    items = [json.loads(l) for l in lines]
    if clear:
        EVENTS_FILE.write_text("", encoding="utf-8")
    return items[-20:]


def events_remove_ids(ids: set[str]) -> int:
    """推送成功的事件按 id 移除, 避免队列残留导致托盘计数虚高/重复播报。"""
    if not EVENTS_FILE.exists() or not ids:
        return 0
    lines = [l for l in EVENTS_FILE.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    kept = [l for l in lines if json.loads(l).get("id") not in ids]
    EVENTS_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return len(lines) - len(kept)


def _confirmations() -> list[dict]:
    if not CONFIRM_FILE.exists():
        return []
    try:
        return json.loads(CONFIRM_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_confirmations(items: list[dict]) -> None:
    CONFIRM_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def confirm_register(agent: str, question: str, reply_file: str = "", msg_uid: str = "") -> dict:
    items = _confirmations()
    c = {
        "id": uuid.uuid4().hex[:8], "agent": agent,
        "question": (question or "")[:500], "reply_file": reply_file,
        "created": _now(), "answered": False, "msg_uid": msg_uid,
    }
    items.append(c)
    _save_confirmations(items)
    return c


def confirm_pending(agent: str = "") -> list[dict]:
    items = [c for c in _confirmations() if not c.get("answered")]
    if agent:
        items = [c for c in items if c.get("agent") == agent]
    return items


def confirm_get(cid: str) -> dict | None:
    for c in _confirmations():
        if c.get("id") == cid:
            return c
    return None


def confirm_answer(agent: str, answer: str) -> tuple[bool, str]:
    items = _confirmations()
    # 回答该 agent 最近一条待确认(机器人念的是最新问题)
    for c in reversed(items):
        if not c.get("answered") and c.get("agent") == agent:
            c["answered"] = True
            c["answer"] = (answer or "")[:500]
            c["answered_at"] = _now()
            _save_confirmations(items)
            reply_file = c.get("reply_file", "")
            if reply_file:
                try:
                    Path(reply_file).write_text(answer, encoding="utf-8")
                except Exception as e:
                    return True, f"已记录回答, 但回写文件失败: {e}"
            return True, "已回复 " + agent
    return False, "没有该 agent 的待确认问题"


def confirm_answer_by_uid(msg_uid: str, answer: str) -> tuple[bool, str, dict | None]:
    """Phase 9-B: 按 msg_uid 精确回答待确认问题(固件触屏浮层点击回调)。
    返回 (ok, 消息, 被回答的 confirmation 对象)。"""
    if not msg_uid:
        return False, "缺少 msg_uid", None
    items = _confirmations()
    for c in items:
        if not c.get("answered") and c.get("msg_uid") == msg_uid:
            c["answered"] = True
            c["answer"] = (answer or "")[:500]
            c["answered_at"] = _now()
            c["answered_by"] = "touchscreen"
            _save_confirmations(items)
            reply_file = c.get("reply_file", "")
            if reply_file:
                try:
                    Path(reply_file).write_text(answer, encoding="utf-8")
                except Exception as e:
                    return True, f"已记录回答, 但回写文件失败: {e}", c
            return True, "已回复 " + str(c.get("agent", "")), c
    # 兼容旧数据: msg_uid 未登记的确认, 回退到该 agent 最近一条
    return False, f"没有 msg_uid={msg_uid} 的待确认问题", None


# ---------------------------------------------------------------- agent 定义
AGENT_CLIS: dict = {
    "claude": {"cli": "claude", "exec_args": ["-p"], "version_args": ["--version"], "workdir": str(Path.home())},
    "codex": {"cli": "codex", "exec_args": ["exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust"],
              "version_args": ["--version"], "workdir": str(ROOT)},
    "deepseek": {"cli": "pnpm",
                 "exec_args": ["dsh", "--profile", "headless"],
                 "version_args": ["dsh", "--version"],
                 "workdir": str(Path.home() / "deepseek-harness")},
    "agy": {"cli": "agy", "exec_args": ["--print"], "version_args": ["--version"], "workdir": str(ROOT)},
    "pi": {"cli": "pi", "exec_args": ["--print", "--no-session", "--no-context-files"],
           "version_args": ["--version"], "workdir": str(Path.home())},
    "vscode": {"cli": "code", "exec_args": ["-r"], "version_args": ["--version"], "workdir": str(ROOT)},
    # system: 仅网关自检告警使用(agent_event 白名单), 无 CLI 派发
    "system": {"cli": "system", "exec_args": [], "version_args": [], "workdir": str(ROOT)},
}

# 别名归一化: ASR 可能听成同音词/中英混说, 统一映射到标准 agent 名。
AGENT_ALIASES: dict = {
    "antigravity": "agy", "agy": "agy", "反重力": "agy", "安特格拉维蒂": "agy",
    "codex": "codex", "可头大": "codex", "扣代码": "codex", "扣德斯": "codex",
    "deepseek": "deepseek", "dsh": "deepseek", "深寻": "deepseek", "深度求索": "deepseek",
    "claude": "claude", "克劳德": "claude",
    "pi": "pi",
    "vscode": "vscode", "code": "vscode",
}


def normalize_agent(name: str) -> str:
    """把用户/ASR 说出的 agent 名归一化到标准名(未知原样返回)。"""
    n = str(name or "").strip().lower()
    return AGENT_ALIASES.get(n, n)


CREATE_NEW_CONSOLE = 0x00000010 if os.name == "nt" else 0
PROJECT_ROOT = ROOT.parent.parent  # ${STACKCHAN_ROOT}
VISIBLE_DIR = ROOT / "state" / "visible_runs"

# 机器人驱动的任务: 在 agent 自己的可见窗口里执行(用户能看到过程与输出)。
# 注意: codex 不能带 --sandbox workspace-write, 本机沙箱无法 spawn 子进程
# (Windows 错误 5 Access denied), 用全局 danger-full-access 配置即可正常执行。
VISIBLE_SPECS: dict = {
    "codex": {"cmd": ["codex", "exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust"],
              "workdir": str(PROJECT_ROOT), "title": "Codex-Asong"},
    "deepseek": {"cmd": ["pnpm", "dsh", "--profile", "headless"],
                 "workdir": str(Path.home() / "deepseek-harness"), "title": "DeepSeek-Asong"},
    "claude": {"cmd": [sys.executable, str(ROOT.parent / "agents" / "claude_visible_run.py")],
               "workdir": str(PROJECT_ROOT), "title": "ClaudeCode-Asong"},
    "agy": {"cmd": ["agy", "--prompt-interactive"],
            "workdir": str(PROJECT_ROOT), "title": "Antigravity-Asong"},
    "pi": {"cmd": ["pi", "--no-context-files"],
           "workdir": str(PROJECT_ROOT), "title": "pi-Asong"},
}


def spawn_visible(name: str, task: str) -> tuple[bool, str]:
    """在指定 agent 的控制台窗口里后台执行任务(最小化启动, 不抢焦点)。

    任务内容经环境变量 ASON_TASK 传入(Unicode 安全), 窗口标题标明 agent,
    执行结束后窗口保留输出 15 秒(等结果推送给机器人播报)后自动关闭。
    结果回流走各 agent 的 hooks/扩展
    (codex_hook / claude_hook / antigravity fusion / pi hooks-bridge)。
    """
    spec = VISIBLE_SPECS.get(name)
    if not spec:
        return False, f"未知 agent: {name}"
    task = str(task or "").strip()
    if not task:
        return False, "任务内容为空"
    try:
        VISIBLE_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        cmd_file = VISIBLE_DIR / f"{name}-{ts}.cmd"
        quoted = " ".join(f'"{c}"' for c in spec["cmd"])
        content = (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            f"title {spec['title']}\r\n"
            f"cd /d {spec['workdir']}\r\n"
            "echo [Asong] task started: %ASON_TASK%\r\n"
            "echo.\r\n"
            f"{quoted} \"%ASON_TASK%\"\r\n"
            "echo.\r\n"
            "echo [Asong] task finished, result synced to robot\r\n"
            "echo 窗口 15 秒后自动关闭(结果已同步给机器人播报)\r\n"
            "timeout /t 15 /nobreak >nul\r\n"
        )
        cmd_file.write_text(content, encoding="utf-8")
        env = os.environ.copy()
        env["ASON_TASK"] = task
        # 最小化后台启动(SW_SHOWMINNOACTIVE): 不抢焦点, 可手动恢复查看输出
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
        subprocess.Popen(
            ["cmd", "/c", str(cmd_file)],
            cwd=spec["workdir"], env=env,
            creationflags=CREATE_NEW_CONSOLE, close_fds=True, startupinfo=si,
        )
        events_append(name, "progress", f"{name} 任务已在窗口启动: {task[:150]}")
        return True, f"已在 {spec['title']} 窗口启动「{task[:30]}」"
    except Exception as e:
        return False, f"启动可见窗口失败: {e}"


def status_text(name: str) -> str:
    ok, info = probe(name)
    pids = running_processes(name)
    pend = len(confirm_pending(name))
    events = [e for e in events_read(False) if e.get("agent") == name][-3:]
    lines = [f"{name}: {'可用' if ok else '不可用'} ({info})", f"  运行进程: {len(pids)} 个", f"  待确认问题: {pend} 个"]
    for e in events:
        lines.append(f"  最近事件[{e['type']}]: {(e.get('summary') or '')[:60]}")
    return "\n".join(lines)


def status_all_text() -> str:
    """并发探测 4 个 agent, 避免串行 4×(CLI 启动+进程查询) 导致工具超时。"""
    names = list(AGENT_CLIS)
    with ThreadPoolExecutor(max_workers=min(4, len(names))) as ex:
        parts = list(ex.map(status_text, names))
    return "\n".join(parts)


def _speech_clean(text: str, max_len: int = 150) -> str:
    """把 agent 事件摘要清洗成适合语音朗读的文本(去 markdown/路径/长串)。"""
    t = str(text or "")
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)          # [text](url) -> text
    t = re.sub(r"[`*_#>{}\-]{1,}", " ", t)                   # markdown 标记
    t = re.sub(r"file:///\S*", "", t)                        # file:/// URI
    t = re.sub(r"\b[A-Za-z]:[\\/][^\s，。；、)]*", "", t)      # Windows 路径
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"\s+([：，。；、,.;])", r"\1", t)               # 标点前不留空格
    t = t.strip(":：-—·,，。;；")
    return t[:max_len]


def pending_text(clear: bool = False) -> str:
    """机器人端调用: 返回待播报的 agent 事件 + 待确认问题(已口语化)。"""
    parts = []
    for ev in events_read(clear):
        parts.append(f"[{ev['agent']} {ev['type']}] {_speech_clean(ev['summary'])}")
    for c in confirm_pending():
        parts.append(f"[{c['agent']} 待确认] {_speech_clean(c['question'])}")
    return "\n".join(parts) if parts else ""


def query(agent: str, task: str, timeout_s: int = 120, visible: bool = True) -> str:
    """执行 agent 任务。

    visible=True(默认, 机器人驱动): 在 agent 自己的可见窗口执行, 结果由 hooks 回流;
    visible=False: 无头执行并直接返回结果(脚本/自检用)。
    """
    agent = normalize_agent(agent)
    # 工单 3: VS Code 不支持语音派发任务(code -r <task> 会把任务文本当文件打开)
    if agent == "vscode":
        return "VS Code 暂不支持语音派发任务。请在 VS Code 里手动运行任务，任务结束会经 vscode_hook 自动播报。"
    # Fail-Fast 存活预检: agent 未安装/CLI 不可用时立即返回, 严禁死等 timeout_s
    ok, info = probe(agent)
    if not ok:
        return f"[{agent}] 未在电脑启动，请先打开它 ({info})"
    if visible:
        ok2, msg = spawn_visible(agent, task)
        if ok2:
            return msg
        # 窗口启动失败 -> 回退无头执行并返回结果
    res = run_agent(agent, task, timeout_s)
    if res["ok"]:
        out = (res["out"] or "").strip()
        text = out[:2000] if out else f"{agent} 执行成功(无输出)"
    else:
        text = f"{agent} 执行失败(rc={res['rc']}): {(res['err'] or res['out'])[:600]}"
    events_append(agent, "done", text[:300])
    return text
