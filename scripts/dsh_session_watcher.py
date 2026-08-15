# -*- coding: utf-8 -*-
"""dsh_session_watcher.py — DeepSeek Harness(dsh) 会话兜底监听器

DeepSeek Harness Web UI 会话以 zstd 压缩事件流存于 ~/.dsh/sessions/**/session.jsonl.zstd。
本监听器解码事件流, 提取每轮最终 assistant/message 文本, 在 turn/end 时经网关
/api/agent_event 上报 (agent=deepseek, event=done), 机器人播报 + Windows 通知。

防双播:
  1) 跳过 cwd 为 deepseek-harness 目录的会话 —— 机器人 headless 派发的结果已走
     MCP 语音返回, 不重复播报;
  2) 按 轮次+文本哈希 去重 (msg_uid = dshwatch-<session8>-<turn>-<hash8>)。

用法:
  py dsh_session_watcher.py            # 单轮扫描+广播(供网关线程周期调用)
  py dsh_session_watcher.py --dry-run  # 只打印将广播的内容, 不发网络请求
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import zstandard

ROOT = Path(__file__).resolve().parent.parent
GATEWAY_URL = "http://127.0.0.1:8010"
GATEWAY_CFG = ROOT / "gateway" / "config.json"
STATE_FILE = ROOT / "gateway" / "state" / "dsh_session_watcher.state.json"
LOG_FILE = ROOT / "gateway" / "state" / "dsh_session_watcher.log"
DSH_SESSIONS = Path.home() / ".dsh" / "sessions"
DSH_HOME = Path.home() / "deepseek-harness"
POLL_S = 20


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
        return {"initialized": False, "dsh_end": {}}


def _save_state(st: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _post_done(summary: str, session8: str, turn, text_hash: str) -> bool:
    body = json.dumps({
        "agent": "deepseek", "event": "done",
        "summary": summary[:300], "session_id": "dsh-watcher",
        "msg_uid": f"dshwatch-{session8}-{turn}-{text_hash[:8]}",
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
        log(f"POST 失败 {session8} turn={turn}: {e}")
        return False


def _decode(path: Path) -> list[str]:
    """zstd 流式解压 session.jsonl.zstd -> 行列表。"""
    raw = path.read_bytes()
    data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read()
    return data.decode("utf-8", errors="replace").splitlines()


def _extract(lines: list[str]) -> tuple[str, str, dict]:
    """解析事件流 -> (session_id, cwd, {turn: 最终助手文本})。
    每轮取最后一条 assistant/message 的 text 内容。"""
    sid, cwd = "", ""
    turn_texts: dict = {}
    cur_turn, cur_text = None, ""
    for ln in lines:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        t = o.get("type")
        if t == "session":
            sid = str(o.get("id") or "")
            cwd = str(o.get("cwd") or "")
        elif t == "assistant/message":
            d = o.get("data") or {}
            m = d.get("message") or {}
            turn = d.get("turn")
            if turn is None:
                continue
            content = m.get("content") or ""
            txt = ""
            if isinstance(content, str):
                txt = content
            elif isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        txt += str(c.get("text") or "")
            txt = " ".join(txt.split()).strip()
            if txt:
                cur_turn, cur_text = turn, txt
        elif t == "turn/end":
            d = o.get("data") or {}
            turn = d.get("turn")
            if turn is not None and cur_turn == turn and cur_text:
                turn_texts[turn] = cur_text
            cur_turn, cur_text = None, ""
    return sid, cwd, turn_texts


def scan_and_broadcast(dry_run: bool = False) -> list[str]:
    st = _load_state()
    first_run = not st.get("initialized")
    ends = st.setdefault("dsh_end", {})
    done: list[str] = []
    if not DSH_SESSIONS.is_dir():
        return done
    for tp in DSH_SESSIONS.rglob("session.jsonl.zstd"):
        key = str(tp)
        try:
            sid, cwd, turn_texts = _extract(_decode(tp))
        except Exception as e:
            log(f"decode 失败 {key}: {e}")
            continue
        if not sid:
            continue
        # 跳过机器人 headless 派发会话(结果已走 MCP 语音, 防双播)
        if cwd and Path(cwd).resolve() == DSH_HOME.resolve():
            continue
        session8 = sid.replace("session-", "")[:8]
        seen = ends.setdefault(key, {})
        for turn, text in turn_texts.items():
            h = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if str(turn) in seen:
                continue
            if first_run:
                seen[str(turn)] = h  # 首次运行只建基线, 不回放历史
                continue
            if dry_run:
                done.append(f"[dry-run] {session8} turn={turn} text={text[:60]}")
                seen[str(turn)] = h
            elif _post_done(text, session8, turn, h):
                seen[str(turn)] = h
                done.append(f"posted {session8} turn={turn}: {text[:50]}")
        if len(seen) > 100:
            for k in list(seen)[:-100]:
                seen.pop(k, None)
    if first_run:
        st["initialized"] = True
    _save_state(st)
    return done


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    for d in scan_and_broadcast(dry):
        log(d)
        print(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
