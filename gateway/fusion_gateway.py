#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fusion_gateway.py — StackChan x xiaozhi-esp32-server x Codex/Claude Code 融合网关
================================================================================

角色:
  1. 服务端MCP (SERVER_MCP): xiaozhi-esp32-server 通过 streamable-http 连接本网关,
     把以下工具注册给设备 LLM(DeepSeek), 机器人用语音即可驱动:
       - codex_query / claude_query : 让电脑上的 Codex / Claude Code 执行任务
       - robot_pending             : 唤醒后取待播报消息(agent 排队的消息)
       - robot_status / ws_probe   : 分层连通性自检
  2. 标准 MCP server: Codex/Claude Code 也可以作为 MCP 客户端连接本网关:
       - robot_say    : 给机器人排队一条语音播报(机器人下次唤醒时由 LLM 朗读)
       - robot_status : 连通性验证
       - codex_query / claude_query : 手动驱动 agent

传输:
  --transport stdio : 供 Codex CLI / Claude Code 以 stdio MCP 方式使用
  --transport http  : 供 xiaozhi SERVER_MCP (streamable-http) 及 Claude Code (http MCP) 使用
                      HTTP 模式强制 Bearer 认证(fail-closed), /healthz 除外

用法:
  python fusion_gateway.py --transport http --host 0.0.0.0 --port 8010
  python fusion_gateway.py --transport stdio
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

import edge_tts
from mcp.server.fastmcp import FastMCP
import paho.mqtt.client as paho

import agents_core

ROOT = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "ota_url": "https://YOUR_FUNNEL_DOMAIN.ts.net/xiaozhi/ota/",
    "robot_mac": "AA:BB:CC:DD:EE:FF",
    "endpoint_health_url": "http://127.0.0.1:8004/mcp_endpoint/health?key=YOUR_HEALTH_KEY",
    "docker_container": "xiaozhi-esp32-server",
    "docker_log_lookback_minutes": 120,
    "auth_token": "YOUR_GATEWAY_TOKEN",
    "allow_codex": True,
    "allow_claude": True,
    "codex_cli": "codex",
    "claude_cli": "claude",
    "exec_cwd": ".",
    "max_output_chars": 4000,
    "max_timeout_s": 600,
    "http_host": "0.0.0.0",
    "http_port": 8010,
    "push_interval_s": 5,
    "push_mqtt_host": "broker-cn.emqx.io",
    "push_mqtt_port": 1883,
    "push_topic_prefix": "stackchan",
    "push_tts_voice": "zh-HK-HiuMaanNeural",  # 女声粤语曉曼(中英混杂); 备选 ja-JP-NanamiNeural(日语) / zh-TW-HsiaoChenNeural(台普)
    "push_tts_rate": "+0%",  # 播报语速 1.0x(基准), 不再 +20% 提速
    # Phase 9-D: 本地粤语 TTS 兜底 + 离线 LLM
    "tts_cache_size": 200,  # TTS 帧缓存 LRU 容量(按文本 SHA256)
    "tts_fallback_model_dir": "tts_models/vits-cantonese-hf-xiaomaiiwn",  # sherpa-onnx 粤语女声(小美)
    "tts_fallback_speed": 1.0,  # 本地兜底 TTS 语速 1.0x
    "local_llm_host": "http://127.0.0.1:11434",  # Ollama
    "local_llm_model": "qwen3:8b",
}

TOOL_NAMES = [
    "agent_status", "robot_status", "docker_status", "ws_probe",
    "codex_query", "claude_query", "robot_say", "robot_pending",
    "agent_query", "agent_pending", "agent_confirm", "local_query",
    "robot_snap",
]

STARTED_AT = time.time()
PENDING_TTL_SECONDS = 300  # Phase 7.1: 待播报消息 5 分钟 TTL, 根治开机/重启后倒灌旧消息
CFG: dict = {}

# ---- 本机 ⇄ 机器人 连接开关(2026-08-13): 多台电脑共用同一配置时, 只有 attached 的
# 那台才向机器人推流; 断开时消息照常进 pending 队列不丢, 连接后 5s 内自动补推。----
_ROBOT_ATTACH_FILE = Path(__file__).resolve().parent / "state" / "robot_attached.json"
_robot_attached = True
_attach_lock = threading.Lock()


def _load_robot_attached() -> bool:
    try:
        if _ROBOT_ATTACH_FILE.exists():
            return bool(json.loads(_ROBOT_ATTACH_FILE.read_text(encoding="utf-8")).get("attached", True))
    except Exception:
        pass
    return True


def robot_attached() -> bool:
    with _attach_lock:
        return _robot_attached


def set_robot_attached(v: bool) -> bool:
    global _robot_attached
    v = bool(v)
    with _attach_lock:
        _robot_attached = v
        try:
            _ROBOT_ATTACH_FILE.parent.mkdir(parents=True, exist_ok=True)
            _ROBOT_ATTACH_FILE.write_text(json.dumps({"attached": v}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    log(f"robot attached -> {v}")
    return v


_robot_attached = _load_robot_attached()


def _notify_windows(title: str, text: str) -> None:
    """agent 事件(完成/提问/待确认/报错)同步弹 Windows 系统通知(Toast)。
    后台进程异步执行, 不阻塞网关; 失败静默(通知为增强, 非关键路径)。"""
    try:
        helper = Path(__file__).resolve().parent.parent / "scripts" / "notify_windows.ps1"
        if not helper.exists():
            return
        subprocess.Popen(
            ["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe",
             "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
             "-File", str(helper), "-Title", str(title)[:80], "-Text", str(text)[:300]],
            creationflags=_create_no_window(),
        )
    except Exception:
        pass


def _ensure_cfg() -> None:
    """模块级 CFG 在 import 时未初始化(仅在 __main__ 加载), 直接调用工具会
    拿到空配置导致 push_tts_voice 回退成普通话晓晓。这里按需补齐, 保证
    import 场景与主进程行为一致。"""
    global CFG
    if not CFG:
        CFG = load_config()


def log(message: str) -> None:
    try:
        log_file = Path(CFG.get("log_file", ROOT / "gateway.log"))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{_now()}] {message}\n")
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


_PROC_START = _now()  # 进程启动时刻(healthz 上报, 托盘显示), 而非请求当前时间


def _is_expired(created_at: str) -> bool:
    """判断条目是否超过 5 分钟 TTL(解析失败保守保留)。"""
    try:
        ts = datetime.fromisoformat(created_at)
        return (datetime.now(ts.tzinfo) - ts).total_seconds() > PENDING_TTL_SECONDS
    except Exception:
        return False


def load_config(config_path: str | None = None) -> dict:
    env_path = os.environ.get("FUSION_CONFIG", "")
    path = Path(config_path) if config_path else (Path(env_path) if env_path else (ROOT / "config.json"))
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            # 防复发 Fail-Fast: config.json 存在但语法错误, 严禁静默回退默认配置
            log(f"[FATAL] config.json 语法错误, 拒绝启动: {e}")
            raise RuntimeError(f"[FATAL] config.json 语法错误, 拒绝启动: {e}") from e
    if str(cfg.get("exec_cwd", ".")) == ".":
        cfg["exec_cwd"] = str(ROOT)
    cfg["log_file"] = str(ROOT / "gateway.log")
    cfg["pending_file"] = str(ROOT / "state" / "pending.jsonl")
    return cfg


# ---------------------------------------------------------------- 队列
def pending_count() -> int:
    try:
        with open(CFG["pending_file"], encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return 0


def pending_append(text: str, source: str) -> int:
    path = Path(CFG["pending_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": uuid.uuid4().hex[:8], "text": text, "source": source, "created_at": _now(),
        }, ensure_ascii=False) + "\n")
    return pending_count()


def pending_read(clear: bool) -> list[str]:
    path = Path(CFG["pending_file"])
    items: list[str] = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    items.append(f"[{obj.get('created_at', '')}] {obj.get('text', '')}")
                except Exception:
                    items.append(line)
    if clear:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
    return items




def pending_items() -> list[dict]:
    path = Path(CFG["pending_file"])
    items: list[dict] = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    items.append({"id": "", "text": line, "source": "legacy", "created_at": ""})
    # Phase 7.1: 5 分钟 TTL - 丢弃过期旧消息并物理清理, 防重启后倒灌
    fresh = [o for o in items if not _is_expired(o.get("created_at", ""))]
    if len(fresh) != len(items):
        log(f"pending TTL: 丢弃 {len(items) - len(fresh)} 条过期消息(>5min)")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for o in fresh:
                    f.write(json.dumps(o, ensure_ascii=False) + "\n")
        except Exception:
            pass
    return fresh


def pending_remove_ids(ids: set[str]) -> int:
    path = Path(CFG["pending_file"])
    if not path.exists() or not ids:
        return 0
    kept = [o for o in pending_items() if o.get("id") not in ids]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in kept:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return len(kept)


def _tts_text(text: str, limit: int = 180) -> str:
    """把推送文本转成适合 TTS 朗读的形式: 去 markdown 符号/超链接, 压空白, 超长截断。"""
    import re
    t = re.sub(r"[#*_`>|]", " ", str(text or ""))
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)  # markdown 链接 -> 文字
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > limit:
        t = t[:max(0, limit - len("……后面省略。"))].rstrip() + "……后面省略。"
    return t


_FFMPEG = r"ffmpeg"
_push_client: paho.Client | None = None

# ---- 加固 2: ACK 送达闭环(固件收到 START 回发 stackchan/{mac}/ack) ----
_ACK_TIMEOUT_S = 2.0
_RETRY_BACKOFF_S = 30
_acked_texts: dict = {}
_acked_lock = threading.Lock()
_recent_msg_uids: dict = {}  # 幂等记忆: msg_uid -> ts(5 分钟内重复上报静默丢弃)
_photo_lock = threading.Lock()
_photo_state: dict = {}  # robot_snap 单次快照重组状态


def _ack_topic() -> str:
    return f"{CFG.get('push_topic_prefix', 'stackchan')}/{CFG.get('robot_mac', '')}/ack"


def _photo_topic() -> str:
    return f"{CFG.get('push_topic_prefix', 'stackchan')}/{CFG.get('robot_mac', '')}/photo"


def _confirm_topic() -> str:
    return f"{CFG.get('push_topic_prefix', 'stackchan')}/{CFG.get('robot_mac', '')}/confirm"


def _on_ack(client, userdata, msg) -> None:
    """固件 ACK 回执: payload 为 msg_uid(兼容旧固件的文本回执)。"""
    try:
        text = msg.payload.decode("utf-8", "replace")
        with _acked_lock:
            _acked_texts[text] = time.time()
    except Exception:
        pass


def _on_photo(client, userdata, msg) -> None:
    """固件快照回传: [0x01][w:2][h:2] 头 / [0x02][seq:2][data...] 块 / [0x03] 结束。"""
    payload = msg.payload
    if not payload:
        return
    with _photo_lock:
        st = _photo_state
        t = payload[0]
        if t == 1 and len(payload) >= 5:
            st["w"] = (payload[1] << 8) | payload[2]
            st["h"] = (payload[3] << 8) | payload[4]
            st["chunks"] = {}
            st["done"] = False
        elif t == 2 and len(payload) >= 3:
            seq = (payload[1] << 8) | payload[2]
            st.setdefault("chunks", {})[seq] = payload[3:]
        elif t == 3:
            st["done"] = True
            if len(payload) >= 5:
                st["total"] = (payload[1] << 24) | (payload[2] << 16) | (payload[3] << 8) | payload[4]
            ev = st.get("event")
            if ev:
                ev.set()


def _on_confirm(client, userdata, msg) -> None:
    """Phase 9-B: 固件触屏浮层点击回执: payload = msg_uid + \\x01 + allow|deny。
    按 msg_uid 精确回答待确认问题, 并主动播报确认结果。"""
    try:
        payload = msg.payload
        if not payload:
            return
        uid, _, answer = payload.partition(b"\x01")
        uid = uid.decode("utf-8", "replace").strip()
        answer = answer.decode("utf-8", "replace").strip().lower()
        if not uid or answer not in ("allow", "deny"):
            log(f"confirm ignore bad payload: {payload[:60]!r}")
            return
        ok, res, c = agents_core.confirm_answer_by_uid(uid, "允许" if answer == "allow" else "拒绝")
        if not ok:
            log(f"confirm by uid failed: {res}")
            return
        agent = (c or {}).get("agent", "")
        label = "已允许" if answer == "allow" else "已拒绝"
        text = f"{agent} {label}: {(c or {}).get('question', '')[:80]}"
        log(f"confirm ok: {uid} -> {answer} ({res})")
        entry = {"id": uuid.uuid4().hex[:8], "text": text, "source": "confirm",
                 "action": "done", "created_at": _now()}
        path = Path(CFG["pending_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _enqueue_push(text, "confirm", "pending", entry["id"], "done")
        _notify_windows(f"{agent} {label}", str((c or {}).get("question", ""))[:200])
    except Exception as e:
        log(f"confirm handler error: {e}")


def _on_push_message(client, userdata, msg) -> None:
    try:
        topic = msg.topic or ""
        if topic.endswith("/ack"):
            _on_ack(client, userdata, msg)
        elif topic.endswith("/photo"):
            _on_photo(client, userdata, msg)
        elif topic.endswith("/confirm"):
            _on_confirm(client, userdata, msg)
    except Exception:
        pass


def _on_push_connect(client, userdata, flags, rc, properties=None) -> None:
    # paho 重连(clean session)后订阅会丢, 每次连接成功都重订 ACK 主题
    try:
        client.subscribe(_ack_topic(), 0)
        client.subscribe(_photo_topic(), 1)  # QoS1: 照片分块不许丢
        client.subscribe(_confirm_topic(), 1)  # Phase 9-B: 触屏审批回执 QoS1 不丢
    except Exception:
        pass


def _wait_ack(text: str, timeout_s: float = _ACK_TIMEOUT_S) -> bool:
    """等待固件对本次 START 的 ACK; 顺带清理 60s 前的旧 ACK。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with _acked_lock:
            if _acked_texts.get(text):
                return True
        time.sleep(0.1)
    with _acked_lock:
        stale = [k for k, v in _acked_texts.items() if time.time() - v > 60]
        for k in stale:
            del _acked_texts[k]
    return False


def _pending_has_id(entry_id: str) -> bool:
    """pending.jsonl 是否已存在该 id(幂等防护)。"""
    if not entry_id:
        return False
    try:
        path = Path(CFG["pending_file"])
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("id") == entry_id:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _push_mqtt() -> paho.Client:
    global _push_client
    _ensure_cfg()
    if _push_client is None or not _push_client.is_connected():
        try:
            _push_client.loop_stop()  # 清理旧后台 Paho 网络线程, 防断连重建时遗留
        except Exception:
            pass
        # client_id 唯一化(带 pid): 防多实例/测试脚本同 id 互踢导致 ACK 订阅丢失
        _push_client = paho.Client(client_id=f"fusion-gateway-{os.getpid()}", protocol=paho.MQTTv311)
        _push_client.on_message = _on_push_message
        _push_client.on_connect = _on_push_connect
        _push_client.connect(str(CFG.get("push_mqtt_host", "127.0.0.1")), int(CFG.get("push_mqtt_port", 1883)), 10)
        _push_client.loop_start()
        _push_client.subscribe(_ack_topic(), 0)
        _push_client.subscribe(_photo_topic(), 1)
        _push_client.subscribe(_confirm_topic(), 1)
    return _push_client

# ---- 指标 1: 线程安全 Push FIFO + 单 Worker(严禁多 Agent 消息并发交错倾倒) ----
_push_queue: queue.Queue = queue.Queue()
_push_enqueued: set[str] = set()
_enqueue_lock = threading.Lock()


def _enqueue_push(text: str, source: str = "gateway", kind: str = "pending", record_id: str = "", action: str = "") -> None:
    """统一入队(线程安全)。所有主动推送(robot_say / agent_event / _drain_pending)
    必须走这里; 同一 record_id 只允许入队一次, 防轮询并发重复推流。"""
    with _enqueue_lock:
        if record_id and record_id in _push_enqueued:
            return
        if record_id:
            _push_enqueued.add(record_id)
    _push_queue.put({"text": str(text or ""), "source": source, "kind": kind, "id": record_id, "action": action})


def _pending_update(entry_id: str, pushed: bool | None = None, attempted_at: float | None = None) -> None:
    """按 id 更新 pending 条目字段(保留作唤醒补播兜底, 而非删除)。
    机器人唤醒时 robot_pending/agent_pending 朗读后 clear, 或 5 分钟 TTL 清理。"""
    if not entry_id:
        return
    path = Path(CFG["pending_file"])
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        changed = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                out.append(line)
                continue
            if o.get("id") == entry_id:
                if pushed is not None:
                    o["pushed"] = pushed
                if attempted_at is not None:
                    o["attempted_at"] = attempted_at
                changed = True
            out.append(json.dumps(o, ensure_ascii=False))
        if changed:
            path.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
    except Exception:
        pass


def pending_mark_pushed(entry_id: str) -> None:
    """收到机器人 ACK 后标记 pushed(已送达), 保留供唤醒朗读兜底。"""
    _pending_update(entry_id, pushed=True)


def pending_mark_attempted(entry_id: str) -> None:
    """推送尝试后记录 attempted_at, _drain_pending 在退避窗口内不重试。"""
    _pending_update(entry_id, attempted_at=time.time())


def _finish_push_record(kind: str, record_id: str, ok: bool) -> None:
    with _enqueue_lock:
        _push_enqueued.discard(record_id)
    if ok and record_id:
        if kind == "event":
            agents_core.events_remove_ids({record_id})
        elif kind == "pending":
            pending_remove_ids({record_id})  # 规范 3: ACK 确认送达 -> 物理删除(ACK-and-Delete)


def _push_worker() -> None:
    """单 Worker: 逐条出队推流, 前一条播报完毕(音频时长 + 0.5s)才推下一条。
    完整原文经 log 落盘; 失败保留队列供机器人唤醒补播。"""
    while True:
        try:
            item = _push_queue.get()
            if not robot_attached():
                # 本机未连接机器人(多机共用配置): 消息保留不回丢, 连接后自动补推
                if item.get("kind") != "ping":
                    _push_queue.put(item)
                time.sleep(2)
                continue
            if item.get("kind") == "ping":
                # 静默探活: START(action=ping, 空文本) + STOP, 固件收到 START 即回 ACK
                uid = str(item.get("id") or f"ping-{int(time.time())}")
                if _robot_ping_send(uid):
                    log(f"robot ping ack {uid}")
                else:
                    log(f"robot ping no-ack {uid}")
                continue
            text = item.get("text", "")
            record_id = item.get("id", "")
            kind = item.get("kind", "pending")
            action = str(item.get("action") or "")
            msg_uid = str(record_id or "")
            if not text.strip():
                _finish_push_record(kind, record_id, True)
                continue
            ok, err, dur, sent_text = push_send(text, msg_uid, action)
            if ok:
                if _wait_ack(msg_uid or sent_text):
                    log(f"push ack [{item.get('source')}]: {sent_text[:150]}")  # 记录实际播报文本(摘要后)
                    _finish_push_record(kind, record_id, True)  # 已送达 -> 标记 pushed
                else:
                    log(f"push no-ack(机器人离线? 保留兜底): {sent_text[:150]}")
                    _finish_push_record(kind, record_id, False)
                    pending_mark_attempted(record_id)  # 退避期内不重试
            else:
                log(f"push fail: {err} :: {sent_text[:150] or text[:150]}")
                _finish_push_record(kind, record_id, False)
                pending_mark_attempted(record_id)
            # 前一条播完(音频时长+0.5s)再取下一条
            time.sleep(max(0.0, (dur or 0.0) + 0.5))
        except Exception as e:
            log(f"push worker error: {e}")
            time.sleep(1.0)


# 推送音频链路: 16kHz 单声道, 先出 s16le PCM 再转 µ-law(G.711), 60ms/帧 = 960B/帧。
# 不传 Opus: 网关 opus.dll 帧头非标准, ESP 解码器只解 1/6; 传 µ-law 把带宽从
# 32KB/s 减半到 16KB/s, 配合 ESP lwIP 窗口 16KB 后公网 RTT(~0.5s)下仍有余量,
# 根治「报文被 TCP 窗口卡顿导致播报卡顿」。
_PUSH_SAMPLE_RATE = 16000
_PUSH_FRAME_MS = 60
_PUSH_FRAME_BYTES = _PUSH_SAMPLE_RATE * 1 * _PUSH_FRAME_MS // 1000  # 960 (µ-law 1B/采样)
_PUSH_BATCH_FRAMES = 2  # 轨一: 2帧=120ms 音频/批, 报文 ~1.9KB < MTU, 根治 TCP 分片(offset!=0)导致固件丢帧吞字
_PUSH_BATCH_INTERVAL_S = 0.05  # 轻度节流防突发, 不影响实时性(480ms 音频 >> 50ms 间隔)

async def _edge_tts_mp3(text: str, out_path: str) -> None:
    _ensure_cfg()
    comm = edge_tts.Communicate(text, CFG.get("push_tts_voice", "zh-CN-XiaoxiaoNeural"), rate=CFG.get("push_tts_rate", "+0%"))
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            with open(out_path, "ab") as f:
                f.write(chunk["data"])


def _edge_tts_mp3_sync(text: str, out_path: str) -> None:
    """在独立线程中运行 asyncio 协程, 兼容 MCP 工具在事件循环线程内被调用的情况。"""
    result: dict = {}

    def runner() -> None:
        try:
            asyncio.run(_edge_tts_mp3(text, out_path))
            result["ok"] = True
        except Exception as e:
            result["err"] = e

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if result.get("err"):
        raise result["err"]


# ---- Phase 9-D: TTS LRU 缓存 + 本地粤语 TTS 兜底(sherpa-onnx) ----
from collections import OrderedDict

_tts_cache: OrderedDict = OrderedDict()
_tts_cache_lock = threading.Lock()
_sherpa_tts = None
_sherpa_tts_lock = threading.Lock()


def _sherpa_tts_get():
    """懒加载 sherpa-onnx 粤语 TTS(模型 ~108MB, 首次加载 ~0.5-1s, 只加载一次)。"""
    global _sherpa_tts
    _ensure_cfg()
    if _sherpa_tts is not None:
        return _sherpa_tts
    with _sherpa_tts_lock:
        if _sherpa_tts is not None:
            return _sherpa_tts
        import sherpa_onnx
        model_dir = Path(CFG.get("tts_fallback_model_dir", "tts_models/vits-cantonese-hf-xiaomaiiwn"))
        if not model_dir.is_absolute():
            model_dir = ROOT / model_dir
        cfg = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=str(model_dir / "vits-cantonese-hf-xiaomaiiwn.onnx"),
                    lexicon=str(model_dir / "lexicon.txt"),
                    tokens=str(model_dir / "tokens.txt"),
                    data_dir=str(model_dir),
                ),
                num_threads=2,
            ),
            rule_fsts=str(model_dir / "rule.fst"),
            max_num_sentences=1,
        )
        _sherpa_tts = sherpa_onnx.OfflineTts(cfg)
        log(f"sherpa-onnx 粤语 TTS 已加载: {model_dir}")
    return _sherpa_tts


def _sherpa_tts_ulaw_frames(text: str) -> list[bytes]:
    """本地粤语 TTS: sherpa-onnx 出 22050Hz float32 PCM -> ×32767 转 int16
    -> 降到 16kHz -> µ-law, 切成 60ms 帧(960B/帧), 与 EdgeTTS 帧格式完全一致。"""
    tts = _sherpa_tts_get()
    # 语速 +10%: sherpa speed 1.0 偏慢(基准), 用户实测偏快已按基准+10%校准
    audio = tts.generate(text, sid=0, speed=float(CFG.get("tts_fallback_speed", 1.0)))
    samples = getattr(audio, "samples", None)
    if samples is None or len(samples) == 0:
        raise RuntimeError("sherpa-onnx 粤语 TTS 无输出")
    import numpy as np
    pcm16 = (np.asarray(samples, dtype=np.float32) * 32767.0).astype(np.int16)
    raw = pcm16.tobytes()
    # 22050 -> 16000 (audioop.ratecv 整数比例)
    resampled, _ = audioop.ratecv(raw, 2, 1, int(getattr(audio, "sample_rate", 22050)), _PUSH_SAMPLE_RATE, None)
    ulaw = audioop.lin2ulaw(resampled, 2)
    return [ulaw[i:i + _PUSH_FRAME_BYTES]
            for i in range(0, len(ulaw) - len(ulaw) % _PUSH_FRAME_BYTES, _PUSH_FRAME_BYTES)]


def _tts_cache_get(key: str) -> list[bytes] | None:
    with _tts_cache_lock:
        if key in _tts_cache:
            _tts_cache.move_to_end(key)
            return _tts_cache[key]
    return None


def _tts_cache_put(key: str, frames: list[bytes]) -> None:
    with _tts_cache_lock:
        _tts_cache[key] = frames
        _tts_cache.move_to_end(key)
        max_size = max(1, int(CFG.get("tts_cache_size", 200) or 200))
        while len(_tts_cache) > max_size:
            _tts_cache.popitem(last=False)


def _tts_ulaw_frames(text: str) -> list[bytes]:
    """EdgeTTS -> ffmpeg 出 16kHz 单声道 s16le PCM -> µ-law(1B/采样), 切成 60ms 帧(960B/帧)。
    16k 与固件 I2S 输出同频; 固件端 µ-law 查表还原成 s16le 进播放队列出声。
    Phase 9-D: 文本 SHA256 LRU 缓存(默认 200 条); EdgeTTS 失败时回退本地粤语 sherpa-onnx。"""
    key = __import__("hashlib").sha256(text.encode("utf-8")).hexdigest()
    cached = _tts_cache_get(key)
    if cached is not None:
        return cached
    frames: list[bytes] = []
    edge_err: Exception | None = None
    # EdgeTTS 中途断流防护: 实测时长 < 期望(字数×200ms)的 55% 视为截断, 重试一次
    expected_ms = max(1, len(text)) * 200
    for attempt in range(3):
        try:
            mp3 = os.path.join(tempfile.gettempdir(), "fusion_push_tts.mp3")
            if os.path.exists(mp3):
                os.remove(mp3)
            _edge_tts_mp3_sync(text, mp3)
            pcm = subprocess.run([_FFMPEG, "-y", "-i", mp3, "-f", "s16le",
                                  "-ar", str(_PUSH_SAMPLE_RATE), "-ac", "1", "-"],
                                 capture_output=True).stdout
            ulaw = audioop.lin2ulaw(pcm, 2)
            frames = [ulaw[i:i + _PUSH_FRAME_BYTES]
                      for i in range(0, len(ulaw) - len(ulaw) % _PUSH_FRAME_BYTES, _PUSH_FRAME_BYTES)]
            if not frames:
                raise RuntimeError("empty audio")
            actual_ms = len(frames) * _PUSH_FRAME_MS
            if actual_ms < expected_ms * 0.55:
                raise RuntimeError(f"truncated {actual_ms}ms < {expected_ms * 0.55:.0f}ms")
            break
        except Exception as e:
            edge_err = e
            frames = []
            if attempt == 0:
                log(f"EdgeTTS 合成异常/截断, 重试: {e}")
    if not frames:
        # EdgeTTS 失败/空音频/截断 -> 本地粤语兜底; 兜底也失败则明确报错(保留 pending 供补播)
        log(f"EdgeTTS 不可用/截断({edge_err}), 回退本地粤语 TTS: {text[:60]}")
        try:
            frames = _sherpa_tts_ulaw_frames(text)
        except Exception as e:
            log(f"本地 TTS 兜底失败: {e}")
            raise RuntimeError(f"TTS 全部失败(EdgeTTS 截断 + 本地兜底崩溃): {e}") from e
    if not frames:
        raise RuntimeError("TTS 无音频输出")
    _tts_cache_put(key, frames)
    return frames


_DEGENERATE_SUMMARIES = {
    "任务完成", "任务已完成", "项目完成", "项目已完成", "已完成", "完成", "完成啦",
    "搞定", "搞定了", "完毕", "好的", "好", "收到", "知道了", "明白", "好的收到",
    "ok", "okay", "嗯",
}


def _is_degenerate_summary(s: str) -> bool:
    """LLM 摘要退化判定: 过短或只回框架词/无具体内容时判为退化, 丢弃改用尾部结论提取。"""
    t = str(s or "").strip().strip("。.！!？? ")
    if len(t) <= 2:
        return True
    return t in _DEGENERATE_SUMMARIES or t.startswith("任务完成")


def _summarize_for_speech(text: str, max_chars: int = 50) -> str:
    """轨二(吞字根治): 长文本 LLM 口语化摘要, 送入 TTS 引擎前调用。
    len(text) > max_chars 时用本地 LLM(Ollama)提炼为 ≤max_chars 字的口语化摘要;
    【摘要必须包含原文的完整结论】(根因/结果/决定), 可省略过程细节;
    LLM 不可用/超时/输出异常时降级为尾部结论句提取(结论一般在结尾),
    保证推流永不卡死且不丢结论。

    v08.14(2026-08-13): 框架(agent 任务完成/出错/需要确认)与正文分离——
    框架永远保留并拼回, LLM 只对正文做摘要; LLM 输出做退化校验
    (只回"任务完成/好的"等无内容框架词时丢弃, 改尾部结论提取),
    杜绝"只播完成、丢内容"与摘要吞前缀。"""
    text = str(text or "").strip()
    prefix = ""
    m = re.match(
        r"^([A-Za-z0-9_\u4e00-\u9fff]+ (?:任务完成|出错|需要确认))[:：]\s*(.*)$",
        text, re.S)
    if m:
        prefix = m.group(1) + ": "
        text = m.group(2).strip()
    if not text:
        return prefix.rstrip(":： ")
    if len(text) <= max_chars:
        return prefix + text
    host = str(CFG.get("local_llm_host", "http://127.0.0.1:11434"))
    models = [str(CFG.get("local_llm_model", "qwen3.5:9b"))]
    for m in ("qwen3.5:9b", "gemma4:12b"):
        if m not in models:
            models.append(m)
    prompt = (
        f"这是要播报给用户的 agent 任务完成消息正文，请提炼成不超过 {max_chars} 个字的口语化中文摘要。"
        f"硬性要求：1) 必须包含原文的具体内容与最终结论（做了什么/结果是什么/决定是什么），"
        "严禁只输出'任务完成''已完成''好的'等无内容框架词；2) 保留关键数字与专有名词；"
        "3) 口语自然，适合朗读。只输出摘要正文，不要引号、不要markdown、不要任何解释。\n\n"
        f"原文：{text[:800]}"
    )
    for model in models:
        try:
            payload = json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "think": False,
                "options": {"num_predict": max_chars * 2 + 20},
            }).encode("utf-8")
            req = urllib.request.Request(f"{host.rstrip('/')}/api/chat", data=payload,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12.0) as r:
                out = json.loads(r.read().decode("utf-8"))
            content = str((out.get("message") or {}).get("content") or "").strip()
            if not content:
                content = str((out.get("message") or {}).get("thinking") or "").strip()
            content = " ".join(content.split()).strip().strip('"“”「」\'')
            if content and len(content) <= max_chars + 10 and not _is_degenerate_summary(content):
                return prefix + content[:max_chars]
        except Exception:
            continue
    return prefix + _conclusion_fallback(text, max_chars)


def _conclusion_fallback(text: str, max_chars: int) -> str:
    """LLM 摘要不可用时的降级: 结论通常在结尾, 从尾部取完整结论句(≤max_chars),
    替代原先的头部硬截断 text[:max_chars]——避免"只说开头、丢了结论"。"""
    t = " ".join(str(text).split()).strip()
    if len(t) <= max_chars:
        return t
    cand = t
    for sep in ("。", "！", "？", "\n", "；"):
        idx = cand.rfind(sep)
        if idx < 0:
            break
        tail = cand[idx + 1:].strip()
        if 0 < len(tail) <= max_chars and not tail.startswith(("-", "#", "*", "`", ">")):
            return tail
        cand = cand[:idx]
    return t[-max_chars:].lstrip("。！？，；:： ")


def _prewarm_local_llm() -> None:
    """后台预热本地 LLM(Ollama), 避免首次长文本摘要因冷加载超时降级截断。"""
    try:
        host = str(CFG.get("local_llm_host", "http://127.0.0.1:11434"))
        model = str(CFG.get("local_llm_model", "qwen3.5:9b"))
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "回复：好"}],
            "stream": False,
            "think": False,
            "options": {"num_predict": 8},
        }).encode("utf-8")
        req = urllib.request.Request(f"{host.rstrip('/')}/api/chat", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
        log(f"本地 LLM 预热完成: {model}")
    except Exception as e:
        log(f"本地 LLM 预热失败(不影响启动): {e}")


_HOOK_HEALTH_INTERVAL_S = 300  # 5 分钟周期自检(2026-08-11 缩短: 配置漂移/钩子未触发更快发现)


def _hook_health_loop() -> None:
    """周期自检: 每 30 分钟跑一次 scripts/hook_health.py --alert。
    检查并自动修复 Antigravity/Claude/Codex hooks 配置 + 链路自检;
    发现异常/已修复时由该脚本以 agent=system 向机器人推送告警(去重)。
    防 hook 配置漂移导致静默失效(2026-08-10 Antigravity hooks.json 扁平化事故)。"""
    time.sleep(60)  # 启动后 1 分钟首检
    py = r"C:\WINDOWS\py.exe"
    script = str(Path(__file__).resolve().parent.parent / "scripts" / "hook_health.py")
    while True:
        try:
            # --check-only: 周期检查不做链路自检(每 5 分钟 4 条 progress 会刷爆事件队列视图);
            # 链路自检由托盘「Hook 自检与修复」手动执行。
            r = subprocess.run([py, "-3", script, "--alert", "--check-only"],
                               capture_output=True, text=True, timeout=120,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                log(f"hook 自检异常(rc={r.returncode}): {(r.stdout or r.stderr).strip()[:200]}")
            else:
                log("hook 自检正常")
        except Exception as e:
            log(f"hook 自检执行失败: {e}")
        time.sleep(_HOOK_HEALTH_INTERVAL_S)


def _session_watcher_loop() -> None:
    """Codex transcript 兜底监听: 每 5s 扫描, 钩子失效会话(如重启后续传会话)
    的轮次完成后自动推送到网关, 保证 Codex 回复必播(与钩子防双播)。"""
    sys.path.append(str(Path(__file__).resolve().parent.parent / "scripts"))
    try:
        import session_watcher
    except Exception as e:
        log(f"session_watcher 导入失败: {e}")
        return
    while True:
        try:
            for d in session_watcher.scan_and_broadcast():
                log(f"session_watcher: {d}")
        except Exception as e:
            log(f"session_watcher 异常: {e}")
        time.sleep(5)


def push_send(text: str, msg_uid: str = "", action: str = "") -> tuple[bool, str, float, str]:
    """MQTT 主动推送: EdgeTTS 合成 -> 16k µ-law 60ms 帧 -> stackchan/{mac}/push。
    指标 3: 语音推流 ≤60 字口语化摘要(轨二 LLM 提炼, 失败降级截断); 完整原文由调用方保留在
    pending.jsonl 与日志。START 报头: \x01+msg_uid+\x00+action+\x00+text
    (Phase 8.1: action=done/question 驱动固件点头/偏头), 供固件 ACK 回执。
    返回 (ok, err, 音频时长秒, 实际发送文本)。"""
    try:
        # 播报规则: ≤50 字完整播报; >50 字 LLM 口语化摘要为 ≤50 字(失败降级截断)。
        # 先只做清洗不截断(limit 拉高), 保证摘要器拿到完整原文。
        text = _tts_text(text, limit=2000)
        text = _summarize_for_speech(text, max_chars=50)
        topic = f"{CFG.get('push_topic_prefix', 'stackchan')}/{CFG.get('robot_mac', '')}/push"
        client = _push_mqtt()
        # 先合成全部 µ-law 帧, 再发 START: 严禁机器人先进 Speaking 空等 TTS
        # (否则 3s 断流看门狗会误判为断流, 把帧全部丢弃 = 播报被掐掉)
        frames = _tts_ulaw_frames(text)
        # 句尾追加 240ms µ-law 静音缓冲帧(0xFF=静音): 确保最后一个词在硬件 DMA
        # 中完全发声后再发 STOP, 根治句尾吞字。
        silence_frame = b"\xff" * _PUSH_FRAME_BYTES
        frames.extend([silence_frame] * 4)
        # QoS1 + 2帧/条批量: 每批 ~1922B; 固件订阅 QoS1, 公网 EMQX 丢包/断连时 broker 重投,
        # 根治 QoS0 静默丢帧导致的播报吞字(ACK 只证明 START 到达, 音频帧必须靠 QoS1 保送达)
        if msg_uid and action:
            client.publish(topic, b"\x01" + msg_uid.encode("utf-8") + b"\x00" + action.encode("utf-8") + b"\x00" + text.encode("utf-8"), qos=1)
        elif msg_uid:
            client.publish(topic, b"\x01" + msg_uid.encode("utf-8") + b"\x00" + text.encode("utf-8"), qos=1)
        else:
            client.publish(topic, b"\x01" + text.encode("utf-8"), qos=1)
        for i in range(0, len(frames), _PUSH_BATCH_FRAMES):
            if i > 0:
                time.sleep(_PUSH_BATCH_INTERVAL_S)  # 节流: 首包立发, 后续 170ms/批
            batch = frames[i:i + _PUSH_BATCH_FRAMES]
            payload = b"\x02" + bytes([len(batch)]) + b"".join(batch)
            client.publish(topic, payload, qos=1)
        client.publish(topic, b"\x03", qos=1)
        return True, "ok", len(frames) * (_PUSH_FRAME_MS / 1000.0), text
    except Exception as e:
        return False, str(e), 0.0, ""


def _drain_pending() -> tuple[int, int]:
    """把待播报消息统一入队(单 Worker 串行推流); 返回 (入队数, 失败数)。
    同一 record_id 去重, 由 Worker 推送成功后移除; 失败保留供唤醒补播。"""
    if not robot_attached():
        return 0, 0  # 断开期间不入队, 连接后由 _push_loop 自动补推
    items = pending_items()
    enqueued = 0
    now = time.time()
    for o in items:
        if o.get("pushed"):
            continue  # 已推送过, 仅作唤醒补播兜底, 不重复推流
        att = o.get("attempted_at")
        if isinstance(att, (int, float)) and now - att < _RETRY_BACKOFF_S:
            continue  # 推送尝试失败后 30s 退避, 防离线时每 5s 重推刷屏
        text = str(o.get("text", "")).strip()
        if not text:
            continue
        _enqueue_push(text, "pending", "pending", o.get("id", ""), str(o.get("action") or ""))
        enqueued += 1
    return enqueued, 0


def _robot_ping_send(uid: str) -> bool:
    """静默探活: 发 START(action=ping, 空文本) + STOP; 固件收到 START 即回 ACK。
    走 worker 队列串行执行, 不与真实播报并发冲突。"""
    try:
        client = _push_mqtt()
        topic = f"{CFG.get('push_topic_prefix', 'stackchan')}/{CFG.get('robot_mac', '')}/push"
        client.publish(topic, b"\x01" + uid.encode("utf-8") + b"\x00ping\x00", qos=1)
        client.publish(topic, b"\x03", qos=1)
        return _wait_ack(uid, 4.0)
    except Exception:
        return False


def _robot_ping_loop() -> None:
    """每 5 分钟静默探活一次(本机已连接时), 供托盘判断机器人本体在线/离线。"""
    while True:
        try:
            if robot_attached():
                uid = f"ping-{int(time.time())}"
                _push_queue.put({"text": "", "source": "ping", "kind": "ping", "id": uid, "action": "ping"})
        except Exception:
            pass
        time.sleep(300)


def _push_loop() -> None:
    while True:
        try:
            interval = int(CFG.get("push_interval_s", 5) or 0)
            if interval > 0:
                _drain_pending()
                time.sleep(interval)
            else:
                time.sleep(1)
        except Exception as e:
            log(f"push loop error: {e}")
            time.sleep(5)

# ---------------------------------------------------------------- 工具函数
def http_get(url: str, timeout: float = 15.0) -> tuple[int | None, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fusion-gateway/0.1"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except Exception as e:
        return None, str(e)


def _create_no_window() -> int:
    if platform.system() == "Windows":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def resolve_cli(name: str) -> str:
    return shutil.which(name) or name


def run_cli(argv: list[str], timeout_s: int, cwd: str) -> dict:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s, cwd=cwd, stdin=subprocess.DEVNULL, creationflags=_create_no_window(),
        )
        return {"ok": p.returncode == 0, "returncode": p.returncode, "stdout": p.stdout or "", "stderr": p.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"执行超时(>{timeout_s}s)"}
    except Exception as e:
        return {"ok": False, "returncode": -2, "stdout": "", "stderr": str(e)}


def run_agent_cli(cli_name: str, argv: list[str], timeout_s: int) -> dict:
    """运行 node/npm 系 CLI。Windows 上 .cmd/.bat 走 cmd /c, 其余直接 CreateProcess。"""
    cli = resolve_cli(cli_name)
    clean_argv = []
    for a in argv:
        clean_argv.append(str(a).replace('"', "'"))
    if cli.lower().endswith((".cmd", ".bat")):
        full = ["cmd", "/c", cli] + clean_argv
    else:
        full = [cli] + clean_argv
    return run_cli(full, timeout_s, CFG.get("exec_cwd", str(ROOT)))


def _cli_probe(cli_name: str, argv: list[str]) -> tuple[bool, str]:
    res = run_agent_cli(cli_name, argv, 20)
    if res["ok"]:
        first = (res["stdout"] or "").strip().splitlines()
        return True, (first[0][:80] if first else "ok")
    return False, (res["stderr"] or res["stdout"] or "spawn failed").strip()[:200]


def _format_cli_result(name: str, res: dict) -> str:
    maxc = int(CFG.get("max_output_chars", 4000))
    if res["ok"]:
        out = (res["stdout"] or "").strip()
        return out[:maxc] if out else f"{name} 执行成功(无输出)"
    err = (res["stderr"] or res["stdout"] or "未知错误").strip()
    return f"{name} 执行失败(rc={res['returncode']}): {err[:maxc]}"


def docker_logs_since(minutes: int, container: str) -> str:
    if shutil.which("docker") is None:
        return "[docker 不可用]"
    argv = ["docker", "logs", "--since", f"{minutes}m", container]
    res = run_cli(argv, 40, str(ROOT))
    if not res["ok"]:
        return f"[docker logs: rc={res['returncode']} {(res['stderr'] or res['stdout'])[:200]}]"
    return (res["stdout"] or "") + (res["stderr"] or "")


# ---------------------------------------------------------------- MCP 工具
mcp = FastMCP("fusion-gateway")


@mcp.tool()
def robot_status() -> str:
    """分层连通性自检(当前云链路架构, 不依赖已停用的 xiaozhi-esp32-server 容器):
    1) 云桥接 xiaozhi-mcp(mcp_pipe+server, 心跳) 2) 网关 MCP 工具注册
    3) 推送链路(EMQX MQTT µ-law, 最近 push ok) 4) 机器人最近活动(云桥接工具调用)
    5) 自建服务器状态。Codex/Claude/机器人任一方调用。"""
    lines = []
    # 1) 云桥接 xiaozhi-mcp: 进程数 + bridge.err 心跳(≤3min 视为在线)
    bridge_err = ROOT.parent / "xiaozhi-mcp" / "bridge.err"
    procs = 0
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'xiaozhi-mcp' } | Measure-Object | Select-Object -ExpandProperty Count"],
            capture_output=True, text=True, timeout=10, creationflags=_create_no_window())
        out = (ps.stdout or "").strip()
        procs = int(out) if out.isdigit() else 0
    except Exception as e:
        procs = 0
    hb_age = -1.0
    last_call = ""
    if bridge_err.exists():
        hb_age = round((time.time() - bridge_err.stat().st_mtime) / 60.0, 1)
        try:
            tail = bridge_err.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            for line in reversed(tail):
                if "CallToolRequest" in line:
                    last_call = line.strip()[:90]
                    break
        except Exception:
            pass
    bridge_ok = procs >= 1 and 0 <= hb_age <= 3
    lines.append(f"[1] 云桥接(xiaozhi-mcp): {'PASS' if bridge_ok else 'FAIL'} 进程={procs} 心跳={hb_age if hb_age >= 0 else 'N/A'}min")
    # 2) 网关 MCP 工具注册(本网关直出)
    lines.append(f"[2] 网关MCP工具: PASS 工具={len(TOOL_NAMES)}个 ({', '.join(TOOL_NAMES[:6])}{'...' if len(TOOL_NAMES) > 6 else ''})")
    # 3) 推送链路: 最近一次 push ok(EMQX MQTT µ-law 播报)
    last_push = ""
    try:
        log_lines = Path(CFG.get("log_file", ROOT / "gateway.log")).read_text(
            encoding="utf-8", errors="replace").splitlines()[-500:]
        for line in reversed(log_lines):
            if "push ok" in line:
                last_push = line.strip()[:90]
                break
    except Exception:
        pass
    lines.append(f"[3] 推送链路(EMQX MQTT): {'PASS' if last_push else 'FAIL/暂无'} 最近={last_push or '无'}")
    # 4) 机器人最近活动(经云桥接调用网关工具)
    lines.append(f"[4] 机器人最近活动(云桥接): {last_call or '暂无工具调用'}")
    # 5) 自建服务器
    lines.append("[5] 自建服务器(xiaozhi-esp32-server): 已停用(云链路架构不依赖; 播报走 EMQX MQTT 直连)")
    lines.append(f"[6] 网关自身: pid={os.getpid()} pending={pending_count()}")
    return "\n".join(lines)


@mcp.tool()
def docker_status() -> str:
    """查询电脑上 Docker 容器的运行状态(容器名/状态/端口/健康)。用户问 Docker、容器、服务状态时调用。"""
    res = run_cli(["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}"], 30, str(ROOT))
    if not res["ok"]:
        return f"无法查询 Docker: {(res['stderr'] or res['stdout'])[:300]}"
    out = (res["stdout"] or "").strip()
    return out[:int(CFG.get("max_output_chars", 4000))] if out else "没有运行中的容器。"

@mcp.tool()
def ws_probe() -> str:
    """连接一次 Funnel WSS 并按协议发 hello, 验证服务器接受设备连接(收到服务器 hello 即 PASS)。注意: 会短暂占用一个测试会话。"""
    try:
        import asyncio
        import websockets
    except Exception as e:
        return f"WS 探测不可用(未安装 websockets): {e}"
    status, body = http_get(CFG["ota_url"], 15)
    try:
        ws_url = json.loads(body)["websocket"]["url"]
    except Exception as e:
        return f"无法从 OTA 响应解析 websocket 地址: {e}"

    async def _probe() -> str:
        async with websockets.connect(ws_url, open_timeout=10, close_timeout=3) as ws:
            await ws.send(json.dumps({
                "type": "hello", "version": 1, "transport": "websocket",
                "audio_params": {"format": "opus", "sample_rate": 16000, "channels": 1, "frame_duration": 60},
                "client_id": f"fusion-probe-{uuid.uuid4().hex[:8]}",
                "mac_address": "00:00:00:00:00:00",
            }))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                return "FAIL: 5秒内未收到服务器 hello"
            return f"PASS: 收到服务器响应 {str(msg)[:160]}"

    try:
        return asyncio.run(_probe())
    except Exception as e:
        return f"FAIL: {e}"


@mcp.tool()
def codex_query(instruction: str, timeout_s: int = 120) -> str:
    """在电脑上运行 Codex CLI 完成任务并返回结果。机器人端: 用户说「让Codex…」时调用。注意: 本机 Codex 为商店版时可能无法从后台启动。"""
    if not CFG.get("allow_codex", True):
        return "Codex 执行被配置禁用。"
    timeout_s = max(10, min(int(timeout_s), int(CFG.get("max_timeout_s", 600))))
    log(f"codex_query: {instruction[:200]}")
    res = run_agent_cli(CFG.get("codex_cli", "codex"), ["exec", "--skip-git-repo-check", "--dangerously-bypass-hook-trust", instruction], timeout_s)
    return _format_cli_result("Codex", res)


@mcp.tool()
def claude_query(instruction: str, timeout_s: int = 120) -> str:
    """在电脑上运行 Claude Code CLI 完成任务并返回结果。机器人端: 用户说「让Claude…」时调用。"""
    if not CFG.get("allow_claude", True):
        return "Claude Code 执行被配置禁用。"
    timeout_s = max(10, min(int(timeout_s), int(CFG.get("max_timeout_s", 600))))
    log(f"claude_query: {instruction[:200]}")
    res = run_agent_cli(CFG.get("claude_cli", "claude"), ["-p", instruction, "--output-format", "text"], timeout_s)
    return _format_cli_result("Claude Code", res)


@mcp.tool()
def local_query(instruction: str, timeout_s: int = 90) -> str:
    """离线本地大模型(Ollama qwen3:8b)回答通用问题, 不依赖云端 LLM/网络。
    机器人端: 用户问常识/闲聊/本地知识, 或云端链路不可用时调用。返回 ≤max_output_chars 摘要。"""
    host = str(CFG.get("local_llm_host", "http://127.0.0.1:11434"))
    model = str(CFG.get("local_llm_model", "qwen3:8b"))
    timeout_s = max(10, min(int(timeout_s), 300))
    maxc = int(CFG.get("max_output_chars", 4000))
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": str(instruction)}],
        "stream": False,
        "think": False,  # qwen3 默认开思考模式, 内容会进 thinking 字段; 关闭后直接出正文
        "options": {"num_predict": min(maxc // 2, 2048)},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/api/chat", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            out = json.loads(r.read().decode("utf-8"))
        content = str((out.get("message") or {}).get("content") or "").strip()
        if not content:
            content = str((out.get("message") or {}).get("thinking") or "").strip()
        if not content:
            return f"[本地 LLM 空响应: {model}]"
        return content[:maxc]
    except Exception as e:
        return f"本地 LLM 不可用({host}, {model}): {e}"


@mcp.tool()
async def robot_say(text: str) -> str:
    """给机器人排队一条待播报消息(Codex/Claude 侧调用)。进入单 Worker 推送队列串行播报;
    完整原文保留在 pending.jsonl 与日志, 语音只播 ≤60 字口语化摘要。机器人离线时保留队列,
    唤醒后由 robot_pending 朗读。"""
    text = str(text or "").strip()
    if not text:
        return "消息为空。"
    entry = {"id": uuid.uuid4().hex[:8], "text": text, "source": "agent", "created_at": _now()}
    path = Path(CFG["pending_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    n = pending_count()
    _enqueue_push(text, "agent", "pending", entry["id"])
    return f"已进入播报队列，机器人即将播报。(队列剩余 {max(n - 1, 0)} 条)"

@mcp.tool()
def robot_pending(clear: bool = False) -> str:
    """机器人端调用: 获取待播报消息并原样朗读; 读完请再调用一次 clear=true 清除。没有消息时返回空。"""
    items = pending_read(bool(clear))
    if not items:
        return ""
    return "\n".join(items)


@mcp.tool()
def robot_snap(timeout_s: int = 15) -> str:
    """Phase 8.2: 让机器人用板载摄像头拍一张照片(拍桌面/屏幕), 保存到本机并返回图片路径。
    Antigravity / Claude Code / Codex 可直接打开返回的图片文件查看物理实体/屏幕。"""
    try:
        timeout_s = max(5, min(int(timeout_s), 60))
        topic = f"{CFG.get('push_topic_prefix', 'stackchan')}/{CFG.get('robot_mac', '')}/push"
        client = _push_mqtt()
        ev = threading.Event()
        with _photo_lock:
            _photo_state.clear()
            _photo_state["event"] = ev
            _photo_state["chunks"] = {}
            _photo_state["done"] = False
        client.publish(topic, b"\x04", qos=0)
        if not ev.wait(timeout=timeout_s):
            with _photo_lock:
                _photo_state.pop("event", None)
            return "拍照超时(机器人未回传照片), 请确认机器人在线且摄像头正常"
        with _photo_lock:
            chunks = _photo_state.get("chunks", {})
            w = _photo_state.get("w", 0)
            h = _photo_state.get("h", 0)
            total = _photo_state.get("total", 0)
            _photo_state.pop("event", None)
        if not chunks:
            return "拍照失败(未收到图像数据)"
        data = b"".join(chunks[i] for i in sorted(chunks))
        if total and len(data) != total:
            return f"拍照不完整(收到 {len(data)}/{total} 字节), 请重试"
        if not data.startswith(b"\xff\xd8\xff"):
            return f"拍照数据异常(非 JPEG), 请重试"
        snap_dir = Path(CFG["pending_file"]).parent / "snap"
        snap_dir.mkdir(parents=True, exist_ok=True)
        path = snap_dir / f"snap_{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        path.write_bytes(data)
        return f"照片已保存: {path} ({w}x{h}, {len(data)} bytes)"
    except Exception as e:
        return f"robot_snap 失败: {e}"


# ---------------------------------------------------------------- 多 agent 工具
@mcp.tool()
def agent_status(agent: str = "all") -> str:
    """查询本机各 agent(agy/pi/claude/codex) 状态: CLI 可用性/运行进程/待确认问题/最近事件。
    机器人端: 用户问「agent 状态 / 电脑上哪些 agent 能用」时调用。agent 可选 all 或具体名字。"""
    name = agents_core.normalize_agent(agent or "all")
    if name == "all":
        head = f"网关运行中(pid={os.getpid()}, 待播报 {pending_count()} 条)"
        return head + "\n" + agents_core.status_all_text()
    if name not in agents_core.AGENT_CLIS:
        return f"未知 agent: {name} (可选: {', '.join(agents_core.AGENT_CLIS)})"
    return f"网关运行中(pid={os.getpid()}, 待播报 {pending_count()} 条)\n" + agents_core.status_text(name)


@mcp.tool()
def agent_query(agent: str, instruction: str = "", task: str = "", timeout_s: int = 120) -> str:
    """在电脑上运行指定 agent(agy/pi/claude/codex) 执行任务并返回结果。
    机器人端: 用户说「让 agy/pi/claude/codex 查/做…」时调用。长任务结果自动记入待播报事件。"""
    instruction = instruction or task
    name = agents_core.normalize_agent(agent or "")
    if name not in agents_core.AGENT_CLIS:
        return f"未知 agent: {agent} (可选: {', '.join(agents_core.AGENT_CLIS)})"
    timeout_s = max(10, min(int(timeout_s), int(CFG.get("max_timeout_s", 600))))
    log(f"agent_query: {name} :: {instruction[:150]}")
    return agents_core.query(name, instruction, timeout_s)


@mcp.tool()
def agent_pending(clear: bool = False) -> str:
    """获取待播报的 agent 事件与待确认问题(如「claude 需要确认: 是否允许运行命令」)。
    机器人端: 用户问「有没有 agent 消息/待办/谁找我」或回答待确认问题时调用; 读后可再调 clear=true。"""
    # 合并: pending.jsonl 待推送(agent 完成/出错直接入队) + agent 事件 + 待确认问题
    parts = pending_read(bool(clear))
    ev = agents_core.pending_text(bool(clear))
    if ev:
        parts.append(ev)
    return "\n".join(parts)


@mcp.tool()
def agent_confirm(agent: str, answer: str) -> str:
    """把用户的语音回答回写给指定 agent 的待确认问题(确认/拒绝/补充说明)。
    机器人端: 用户对「agent 需要确认」的问题给出回答后调用。"""
    name = agents_core.normalize_agent(agent or "")
    ok, msg = agents_core.confirm_answer(name, answer)
    return msg


# ---------------------------------------------------------------- HTTP 模式
def build_http_app():
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    mcp_app = mcp.streamable_http_app()

    async def healthz(request):
        return JSONResponse({
            "status": "ok",
            "pid": os.getpid(),
            "started_at": _PROC_START,
            "pending": pending_count(),
            "attached": robot_attached(),
            "tools": TOOL_NAMES,
        })

    async def robot_attach(request):
        """本机 ⇄ 机器人 连接开关: POST {"attached": true|false}。
        断开: 消息继续入 pending 队列但不推 MQTT; 连接: 5s 内自动补推。"""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        v = bool(data.get("attached"))
        set_robot_attached(v)
        return JSONResponse({"ok": True, "attached": robot_attached(), "pending": pending_count()})

    async def agent_event(request):
        """agent hook/包装器上报事件: {agent, event: done|question|progress|error, summary, session_id, reply_file}"""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        agent = agents_core.normalize_agent(str(data.get("agent", "")))
        etype = str(data.get("event", ""))
        summary = str(data.get("summary", ""))
        reply_file = str(data.get("reply_file", ""))
        session_id = str(data.get("session_id", ""))
        msg_uid = str(data.get("msg_uid") or "")
        if agent not in agents_core.AGENT_CLIS and agent != "antigravity":
            return JSONResponse({"error": f"unknown agent: {agent}"}, status_code=400)
        # 规范 2: 幂等防护 —— 同一 msg_uid 已在队列或 5 分钟内处理过, 静默返回 200
        if msg_uid:
            dup = False
            now = time.time()
            with _enqueue_lock:
                stale = [k for k, v in _recent_msg_uids.items() if now - v > 300]
                for k in stale:
                    del _recent_msg_uids[k]
                dup = msg_uid in _recent_msg_uids
                if not dup:
                    _recent_msg_uids[msg_uid] = now
            if dup or _pending_has_id(msg_uid):
                return JSONResponse({"ok": True, "dup": True, "pending": pending_count()})
        if etype == "question":
            c = agents_core.confirm_register(agent, summary, reply_file, msg_uid)
            # 立即入队主动发声: agent 弹窗求确认时机器人桌头主动提醒, 不再静默等唤醒朗读
            text = f"{agent} 需要确认: {summary}"
            entry = {"id": msg_uid or uuid.uuid4().hex[:8], "text": text[:300], "source": "agent",
                     "action": "question", "created_at": _now()}
            path = Path(CFG["pending_file"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _enqueue_push(text, "agent", "pending", entry["id"], "question")
            _notify_windows(f"{agent} 需要确认", summary[:200])
            return JSONResponse({"ok": True, "confirmation_id": c["id"], "pending": pending_count()})
        if etype not in ("done", "progress", "error"):
            return JSONResponse({"error": f"unknown event: {etype}"}, status_code=400)
        if etype in ("done", "error"):
            # 指标(antigravity): agent 完成/出错必须立即进入 pending 队列并直接入队推流,
            # 托盘可见、机器人播报; 失败保留队列供唤醒补播。progress 仍只写 events。
            label = "任务完成" if etype == "done" else "出错"
            text = f"{agent} {label}: {summary}"
            entry = {"id": msg_uid or uuid.uuid4().hex[:8], "text": text, "source": "agent",
                     "action": "done", "created_at": _now()}
            path = Path(CFG["pending_file"])
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _enqueue_push(text, "agent", "pending", entry["id"], "done")
            _notify_windows(f"{agent} {label}", summary[:200])
        else:
            agents_core.events_append(agent, etype, summary, session_id)
        return JSONResponse({"ok": True, "pending": pending_count()})

    async def confirm_status(request):
        """confirm_mcp(宿主)轮询: 按 confirmation_id 查是否已回答。"""
        cid = request.query_params.get("id", "")
        c = agents_core.confirm_get(cid)
        if not c:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "id": cid,
            "answered": bool(c.get("answered")),
            "answer": str(c.get("answer", "")),
        })

    app = Starlette(routes=[
        Route("/healthz", healthz),
        Route("/api/robot_attach", robot_attach, methods=["POST"]),
        Route("/api/agent_event", agent_event, methods=["POST"]),
        Route("/api/agent/confirm_status", confirm_status),
        Mount("/", app=mcp_app),
    ])
    # 关键: 传播内层 MCP app 的 lifespan, 否则会话管理器任务组不会启动
    app.router.lifespan_context = mcp_app.router.lifespan_context

    token = str(CFG.get("auth_token") or "")
    if token:
        class AuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path == "/healthz":
                    return await call_next(request)
                if request.headers.get("Authorization") != f"Bearer {token}":
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(AuthMiddleware)
    return app


def _heartbeat_loop() -> None:
    while True:
        try:
            hb = Path(CFG.get("log_file", ROOT / "gateway.log")).parent / "heartbeat.json"
            hb.write_text(json.dumps({
                "pid": os.getpid(), "ts": _now(), "pending": pending_count(),
                "tools": TOOL_NAMES,
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        time.sleep(30)


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="StackChan Fusion Gateway")
    ap.add_argument("--transport", choices=["stdio", "http"], default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    global CFG
    CFG = load_config(args.config)
    CFG["_config_path"] = str(args.config) if args.config else "gateway/config.json"

    transport = args.transport or "http"
    if transport == "stdio":
        log("starting stdio transport")
        mcp.run(transport="stdio")
        return

    host = args.host or str(CFG.get("http_host", "0.0.0.0"))
    port = args.port or int(CFG.get("http_port", 8010))
    log(f"starting http transport on {host}:{port}")
    # 传输安全: 允许容器经 tailscale IP 访问, 否则 Host 校验返回 421
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{host}:{port}", "127.0.0.1:*", "localhost:*", "YOUR_TAILSCALE_IP:*"],
        )
    except Exception as e:
        log(f"transport security config error: {e}")
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    threading.Thread(target=_push_worker, daemon=True).start()  # 单 Worker 串行推流(指标 1)
    threading.Thread(target=_robot_ping_loop, daemon=True).start()  # 每 5 分钟静默探活(托盘判断机器人在线)
    threading.Thread(target=_prewarm_local_llm, daemon=True).start()  # 预热本地 LLM, 首次长文本摘要不降级截断
    threading.Thread(target=_hook_health_loop, daemon=True).start()  # 5 分钟周期自检(hook 配置漂移自动修复+告警)
    threading.Thread(target=_session_watcher_loop, daemon=True).start()  # Codex transcript 兜底播报
    if int(CFG.get("push_interval_s", 5) or 0) > 0:
        threading.Thread(target=_push_loop, daemon=True).start()

    import uvicorn
    uvicorn.run(build_http_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
