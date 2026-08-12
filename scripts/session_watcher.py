# -*- coding: utf-8 -*-
"""session_watcher.py — Codex 会话 transcript 兜底监听器(保底播报)

背景: 2026-08-12 起 Codex 应用重启后, 续传的旧会话(如 019fd205)不再触发 Stop
钩子(新会话正常), 导致 Codex 助手回复不进播报队列。本监听器直接盯
~/.codex/sessions/**/rollout-*.jsonl, 在轮次完成后把最终助手文本推送到网关
(agent=codex, event=done), 与钩子互为兜底。

防双播:
  1) 某会话近期(codex_hook.log 15 分钟内)有 `posted done ok codex_{session8}` 记录
     -> 判定该会话钩子正常, 监听器跳过(避免与钩子双播);
  2) msg_uid = watcher-<turn_id> 状态去重。

用法:
  py session_watcher.py --dry-run   # 只打印将广播的内容, 不发网络请求
  py session_watcher.py             # 单轮扫描+广播(供网关线程周期调用)
  py session_watcher.py --loop      # 独立常驻(每 5s 一次)
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATEWAY_URL = "http://127.0.0.1:8010"
GATEWAY_CFG = ROOT / "gateway" / "config.json"
HOOK_LOG = ROOT / "gateway" / "state" / "codex_hook.log"
HOOK_STATE = ROOT / "gateway" / "state" / "codex_hook.state.json"
STATE_FILE = ROOT / "gateway" / "state" / "session_watcher.state.json"
LOG_FILE = ROOT / "gateway" / "state" / "session_watcher.log"
CODE_SESSIONS = Path.home() / ".codex" / "sessions"

IDLE_MS = 25000          # 文件超过 25s 无写入视为轮次完成(防长工具调用误报)
POLL_S = 5               # 扫描间隔(秒)
HOOK_ACTIVE_WINDOW_S = 15 * 60  # 会话 15 分钟内有钩子上报 -> 视为钩子正常, 监听器跳过
_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def log(line: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {line}\n")
    except Exception:
        pass


def _load_token() -> str:
    try:
        return json.loads(GATEWAY_CFG.read_text(encoding="utf-8-sig")).get("auth_token", "")
    except Exception:
        return ""


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"offsets": {}, "last_turns": {}, "broadcast": {}, "initialized": False}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        b = st.setdefault("broadcast", {})
        if len(b) > 500:
            for k in list(b)[:-500]:
                b.pop(k, None)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _post_done(summary: str, turn_id: str) -> bool:
    body = json.dumps({
        "agent": "codex", "event": "done",
        "summary": summary[:200], "session_id": "watcher",
        "msg_uid": f"watcher-{turn_id}",
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY_URL + "/api/agent_event", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_load_token()}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return True
    except Exception as e:
        log(f"POST 失败 {turn_id}: {e}")
        return False


def _session8_from_path(tp: Path) -> str:
    m = _UUID_RE.search(tp.name)
    return (m.group(1)[:8] if m else tp.name[:8])


def _hook_recently_posted(session8: str) -> bool:
    """该会话近期(10 分钟内)是否有「真实」钩子 done 上报 -> 钩子正常则跳过 watcher 播报。

    双条件都满足才算钩子健康:
      1) codex_hook.state.json 存在 codex_{session8}_* 且时间戳在 600s 内(钩子确实 post 过);
      2) 该会话最近一条 Stop 日志行带 transcript_path(真实钩子事件)。
    手工/模拟报文(如 Antigravity 复测)的 Stop 只有 session_id/cwd、无 transcript_path,
    不满足条件 2, 避免把已失效的续传会话误判为健康导致 watcher 不播报。
    """
    try:
        recent = False
        if HOOK_STATE.exists():
            st = json.loads(HOOK_STATE.read_text(encoding="utf-8"))
            now = time.time()
            recent = any(
                k.startswith(f"codex_{session8}_") and now - float(v) < HOOK_ACTIVE_WINDOW_S
                for k, v in st.items()
            )
        if not recent or not HOOK_LOG.exists():
            return False
        size = HOOK_LOG.stat().st_size
        with open(HOOK_LOG, "rb") as f:
            f.seek(max(0, size - 131072))
            data = f.read().decode("utf-8", errors="replace")
        # 从日志尾部找该会话最近一条 Stop 事件: 真实钩子带 transcript_path
        for ln in reversed(data.splitlines()[-200:]):
            if ln.startswith("Stop session=") and f"session={session8}" in ln:
                return "transcript_path" in ln
        return False
    except Exception:
        return False


def _extract_turn(line: str) -> tuple[str | None, str | None]:
    """从 transcript 行提取 (turn_id, 助手最终文本); 非助手消息返回 (None, None)。"""
    try:
        o = json.loads(line)
    except Exception:
        return None, None
    if o.get("type") != "response_item":
        return None, None
    p = o.get("payload") or {}
    if p.get("type") != "message" or p.get("role") != "assistant":
        return None, None
    meta = p.get("internal_chat_message_metadata_passthrough") or {}
    turn_id = meta.get("turn_id")
    parts = []
    for c in p.get("content") or []:
        if isinstance(c, dict) and c.get("type") in ("text", "output_text"):
            t = str(c.get("text") or "").strip()
            if t:
                parts.append(t)
    return (turn_id, " ".join(parts)) if turn_id else (None, None)


def scan_and_broadcast(dry_run: bool = False) -> list[str]:
    st = _load_state()
    offsets = st.setdefault("offsets", {})
    last_turns = st.setdefault("last_turns", {})
    broadcast = st.setdefault("broadcast", {})
    first_run = not st.get("initialized")
    done: list[str] = []
    now_ms = time.time() * 1000
    if not CODE_SESSIONS.is_dir():
        return done

    pending_all: list[tuple[str, str, str]] = []  # (session8, turn, text)

    for tp in CODE_SESSIONS.rglob("rollout-*.jsonl"):
        key = str(tp)
        try:
            size = tp.stat().st_size
            off = int(offsets.get(key, 0))
            if off > size:
                off = 0  # 文件被轮转/重写
            with open(tp, "rb") as f:
                f.seek(off)
                data = f.read(size - off).decode("utf-8", errors="replace")
            offsets[key] = size
            session8 = _session8_from_path(tp)
            prev = last_turns.get(key) or {}
            cur_turn = prev.get("turn")
            cur_text = prev.get("text", "")
            if data.strip():
                for ln in data.splitlines():
                    turn, text = _extract_turn(ln)
                    if turn is None:
                        continue
                    if cur_turn is not None and turn != cur_turn:
                        # 新轮次开始: 上一轮完成(首次运行不回放历史)
                        if not first_run and cur_text.strip():
                            pending_all.append((session8, cur_turn, cur_text.strip()))
                        cur_turn, cur_text = turn, text
                    else:
                        cur_turn, cur_text = turn, text
            # 无论是否有新数据, 都检查静默完成(轮次结束后文件不再增长)
            idle = (now_ms - tp.stat().st_mtime * 1000) > IDLE_MS
            if cur_turn is not None and cur_text.strip() and idle and not first_run:
                pending_all.append((session8, cur_turn, cur_text.strip()))
            last_turns[key] = {"turn": cur_turn, "text": cur_text}
        except Exception as e:
            log(f"扫描失败 {key}: {e}")

    if first_run:
        pending_all = []  # 首次运行只建基线, 不回放历史
        st["initialized"] = True
    else:
        # 钩子正常会话(近期有 done 上报)跳过, 避免与钩子双播
        pending_all = [p for p in pending_all if not _hook_recently_posted(p[0])]

    for _session8, turn, text in pending_all:
        if turn in broadcast:
            continue
        if dry_run:
            done.append(f"[dry-run] turn={turn[:8]} text={text[:60]}")
            broadcast[turn] = text[:50]
        elif _post_done(text, turn):
            broadcast[turn] = text[:50]
            done.append(f"posted {turn[:8]}: {text[:50]}")
    _save_state(st)
    return done


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    loop = "--loop" in args
    if loop:
        while True:
            for d in scan_and_broadcast(dry):
                log(d)
            time.sleep(POLL_S)
        return 0
    for d in scan_and_broadcast(dry):
        log(d)
        print(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
