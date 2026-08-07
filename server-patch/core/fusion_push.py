# -*- coding: utf-8 -*-
"""fusion_push — 融合推送补丁 (xiaozhi-esp32-server 0.9.6)

注册设备连接(按 device-id/MAC 与 client-id), 供 /api/push 通过设备既有 WS 连接
用服务器自己的 TTS 管线把文本推给设备播放。无需修改设备固件:
设备空闲时收到 tts state=start -> opus 音频帧 -> state=stop 即进入 Speaking 播放
(固件 2.2.6 application.cc OnIncomingJson/OnIncomingAudio 源码实证)。
"""
import asyncio
import uuid

CONNECTIONS = {}


def register(conn):
    try:
        key = str(getattr(conn, "device_id", "") or "").strip().lower()
        if key:
            CONNECTIONS[key] = conn
        cid = str(getattr(conn, "client_id", "") or "").strip().lower()
        if cid:
            CONNECTIONS["client:" + cid] = conn
    except Exception:
        pass


def unregister(conn):
    try:
        for k in [k for k, v in list(CONNECTIONS.items()) if v is conn]:
            CONNECTIONS.pop(k, None)
    except Exception:
        pass


def find(mac):
    return CONNECTIONS.get(str(mac or "").strip().lower())


async def push_text(conn, text):
    """通过设备既有 WS 连接推送 TTS 播报(无需设备唤醒)。返回 bool。"""
    from core.handle.sendAudioHandle import sendAudioMessage, send_tts_message
    from core.providers.tts.dto.dto import SentenceType
    from core.utils.util import audio_bytes_to_data_stream

    # 推送播报不结束会话: 保持连接, 供连续推送/后续对话复用
    try:
        conn.close_after_chat = False
    except Exception:
        pass

    ws = getattr(conn, "websocket", None)
    if ws is None or getattr(ws, "closed", False):
        return False

    # GLM-Realtime: 推送前打断实时会话, 避免双声道抢播
    if getattr(conn.llm, "is_realtime", False) and hasattr(conn.llm, "cancel"):
        try:
            await conn.llm.cancel()
        except Exception:
            pass

    # 等待 TTS 初始化(最多 3 秒)
    for _ in range(30):
        if getattr(conn, "tts", None) is not None:
            break
        await asyncio.sleep(0.1)
    else:
        return False

    audio_bytes = await conn.tts.text_to_speak(text, None)
    if not audio_bytes:
        return False

    opus_packets = []
    audio_bytes_to_data_stream(
        audio_bytes,
        file_type=getattr(conn.tts, "audio_file_type", "mp3"),
        is_opus=True,
        callback=lambda data: opus_packets.append(data),
        sample_rate=int(getattr(conn, "sample_rate", 16000) or 16000),
    )
    if not opus_packets:
        return False

    await send_tts_message(conn, "start")
    conn.sentence_id = uuid.uuid4().hex
    await sendAudioMessage(conn, SentenceType.FIRST, opus_packets, text)
    await sendAudioMessage(conn, SentenceType.LAST, [], None)
    # GLM-Realtime: 推送结束后恢复实时会话 sentence_id, 后续音频不被丢弃
    if getattr(conn.llm, "is_realtime", False) and hasattr(conn.llm, "resync_sentence_id"):
        try:
            conn.llm.resync_sentence_id()
        except Exception:
            pass
    return True
