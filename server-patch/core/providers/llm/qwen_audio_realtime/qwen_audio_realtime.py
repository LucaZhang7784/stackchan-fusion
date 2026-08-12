"""Qwen-Audio-3.0-Realtime 实时语音 LLM (阿里云百炼 / DashScope)

端到端实时双工语音: 设备 PCM(16k) -> input_audio_buffer.append(裸PCM base64)
-> 语义 VAD(smart_turn, 自动判停/不误打断) -> response.audio.delta(24k PCM)
-> 重采样 16k -> opus -> 设备。

选择理由 (2026-08 调研):
- 阿里百炼官方 WebSocket 实时语音 API, 中国大陆直连, 无需翻墙;
- 语义 VAD(smart_turn) 解决 GLM server_vad 判停不可靠的问题;
- 支持 Function Calling, 可注入网关 MCP 工具;
- 复用现有 DashScope API Key。

参考: aliyun/alibabacloud-bailian-speech-demo
  (samples/conversation/fun-audiochat-realtime)
"""

import asyncio
import base64
import json
import time
import uuid

import numpy as np
import opuslib_next
import websockets

from config.logger import setup_logging
from core.providers.llm.base import LLMProviderBase
from core.providers.tts.dto.dto import SentenceType

TAG = __name__
logger = setup_logging()

DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEVICE_SAMPLE_RATE = 16000
OPUS_FRAME = 960  # 60ms @16k
CHUNK_MS = 100
CHUNK_BYTES = DEVICE_SAMPLE_RATE * 2 * CHUNK_MS // 1000  # 3200 bytes @16k mono16


class StreamingResampler:
    """24k -> 16k 流式线性插值重采样 (保留全缓冲, 位置连续, 无边界毛刺)。"""

    def __init__(self, in_rate=24000, out_rate=DEVICE_SAMPLE_RATE):
        self.step = in_rate / out_rate
        self._buf = b""
        self._out_count = 0

    def reset(self):
        self._buf = b""
        self._out_count = 0

    def feed(self, pcm16: bytes) -> bytes:
        if not pcm16:
            return b""
        self._buf += pcm16
        n_in = len(self._buf) // 2
        if n_in < 2:
            return b""
        n_out = int((n_in - 1) / self.step) + 1
        if n_out <= self._out_count:
            return b""
        y = np.frombuffer(self._buf, dtype="<i2").astype(np.float32)
        positions = np.arange(self._out_count, n_out) * self.step
        out = np.interp(positions, np.arange(n_in), y).astype("<i2").tobytes()
        self._out_count = n_out
        return out


class LLMProvider(LLMProviderBase):
    """Qwen-Audio-3.0-Realtime 实时语音 provider (OpenAI Realtime 同构协议)。"""

    is_realtime = True

    def __init__(self, config):
        self.api_key = str(config.get("api_key") or "")
        self.base_url = str(config.get("base_url") or DEFAULT_URL)
        self.model = str(config.get("model") or "qwen-audio-3.0-realtime-flash")
        self.voice = str(config.get("voice") or "longanqian")
        self.instructions = str(config.get("instructions") or "")
        self.tools_enabled = bool(config.get("tools", True))
        self.turn_detection = str(config.get("turn_detection") or "smart_turn")
        self.conn = None
        self._ws = None
        self._task = None
        self._closing = False
        self._reconnect_attempts = 0
        self._sentence_id = None
        self._resampler = StreamingResampler()
        self._opus = opuslib_next.Encoder(DEVICE_SAMPLE_RATE, 1, "voip")
        self._transcript = ""
        self._tool_queue = []
        self._response_in_progress = False
        self._emotion_done = False
        self._tools_refresh_task = None
        self._audio_count = 0
        self._last_audio_log = 0.0
        self._audio_delta_count = 0
        self._send_buf = b""
        self._opus_packets = 0
        self._user_transcript = ""
        self._last_stt_display = 0.0
        self._last_assistant_display = 0.0

    # ---- 文本接口占位 (实时模式下不会走 chat()) ----
    def response(self, session_id, dialogue):
        return iter(())

    def response_with_functions(self, session_id, dialogue, functions=None):
        return iter(())

    # ---- 生命周期 ----
    async def start(self, conn):
        self.conn = conn
        self._closing = False
        self._sentence_id = uuid.uuid4().hex
        conn.sentence_id = self._sentence_id
        self._task = asyncio.create_task(self._run())
        return self._task

    async def stop(self):
        self._closing = True
        if self._tools_refresh_task and not self._tools_refresh_task.done():
            self._tools_refresh_task.cancel()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._cleanup_ws()

    def _ws_ok(self) -> bool:
        """websockets 14 的 ClientConnection 无 .closed 属性, 用 close_code 判断。"""
        ws = self._ws
        if ws is None:
            return False
        try:
            return ws.close_code is None
        except Exception:
            return False

    def resync_sentence_id(self):
        """fusion_push 播放结束后恢复实时链路 sentence_id。"""
        if self.conn and self._sentence_id:
            self.conn.sentence_id = self._sentence_id

    async def cancel(self):
        """打断: 取消当前响应并清空输入缓冲 (双击/推送触发)。"""
        if self._ws_ok():
            if self._response_in_progress:
                try:
                    await self._send(
                        {
                            "type": "response.cancel",
                            "event_id": uuid.uuid4().hex,
                        }
                    )
                except Exception as e:
                    logger.bind(tag=TAG).debug(f"cancel 发送失败: {e}")
            try:
                await self._send(
                    {
                        "type": "input_audio_buffer.clear",
                        "event_id": uuid.uuid4().hex,
                    }
                )
            except Exception as e:
                logger.bind(tag=TAG).debug(f"清空输入缓冲失败: {e}")
            logger.bind(tag=TAG).info(
                f"Qwen cancel: response_in_progress={self._response_in_progress}, 缓冲已清空"
            )
        self._resampler.reset()
        self._transcript = ""
        self._tool_queue = []
        self._response_in_progress = False
        if self.conn:
            self.conn.client_abort = False

    # ---- 音频上行 (裸 PCM base64, 100ms 分块) ----
    async def send_audio(self, pcm_frame: bytes):
        if not self._ws_ok() or not pcm_frame:
            return
        if getattr(self.conn, "client_abort", False):
            return
        self._send_buf += pcm_frame
        try:
            while len(self._send_buf) >= CHUNK_BYTES:
                chunk = self._send_buf[:CHUNK_BYTES]
                self._send_buf = self._send_buf[CHUNK_BYTES:]
                await self._send(
                    {
                        "type": "input_audio_buffer.append",
                        "event_id": uuid.uuid4().hex,
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }
                )
                self._audio_count += 1
                now = time.time()
                if now - self._last_audio_log >= 5:
                    self._last_audio_log = now
                    logger.bind(tag=TAG).info(
                        f"Qwen 音频上行: 累计 {self._audio_count} 块 (100ms/块)"
                    )
        except Exception as e:
            logger.bind(tag=TAG).debug(f"上传音频失败: {e}")

    async def commit_and_respond(self):
        """备用: client_vad 模式下的判停触发 (smart_turn 默认不需要)。"""
        if not self._ws_ok():
            return
        try:
            await self._send({"type": "input_audio_buffer.commit"})
            await self._send(
                {
                    "type": "response.create",
                    "event_id": uuid.uuid4().hex,
                    "client_timestamp": int(time.time() * 1000),
                }
            )
            logger.bind(tag=TAG).info("Qwen commit + response.create 已发送")
        except Exception as e:
            logger.bind(tag=TAG).warning(f"commit_and_respond 失败: {e}")

    async def clear_buffer(self):
        if not self._ws_ok():
            return
        try:
            await self._send({"type": "input_audio_buffer.clear"})
        except Exception as e:
            logger.bind(tag=TAG).debug(f"clear_buffer 失败: {e}")

    async def _send(self, payload: dict):
        if not self._ws_ok():
            return
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    # ---- 主循环 ----
    async def _run(self):
        while not self._closing:
            try:
                await self._connect_and_loop()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.bind(tag=TAG).error(f"Qwen-Realtime 连接异常: {e}")
            if self._closing:
                break
            self._reconnect_attempts += 1
            if self._reconnect_attempts > 5:
                logger.bind(tag=TAG).error("Qwen-Realtime 连续重连失败, 停止重连")
                break
            delay = min(2**self._reconnect_attempts, 15)
            logger.bind(tag=TAG).info(f"Qwen-Realtime {delay}s 后重连 (第 {self._reconnect_attempts} 次)")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _connect_and_loop(self):
        if not self.api_key:
            logger.bind(tag=TAG).error("Qwen-Realtime 未配置 api_key, 无法连接")
            return
        url = f"{self.base_url}?model={self.model}"
        try:
            # 必须用 websockets 库: aiohttp 客户端经 WARP/代理时音频帧
            # 无法被服务端 VAD 处理 (只回 session 事件, 无语音响应)。
            self._ws = await websockets.connect(
                url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                ping_interval=20,
            )
            self._reconnect_attempts = 0
            await self._send_session_update()
            logger.bind(tag=TAG).info(f"Qwen-Realtime 已连接: {self.model}")
            self._tools_refresh_task = asyncio.create_task(self._refresh_tools_later())
            async for raw in self._ws:
                if self._closing:
                    break
                if isinstance(raw, str):
                    try:
                        await self._handle_message(json.loads(raw))
                    except Exception as e:
                        logger.bind(tag=TAG).warning(f"Qwen 消息处理失败: {e}")
        except websockets.exceptions.ConnectionClosed as e:
            logger.bind(tag=TAG).warning(f"Qwen WS 断开: {e.code} {e.reason}")
        finally:
            await self._cleanup_ws()

    async def _cleanup_ws(self):
        try:
            if self._ws_ok():
                await self._ws.close()
        except Exception:
            pass
        self._ws = None

    async def _send_session_update(self):
        instructions = (
            self.instructions
            or getattr(self.conn, "prompt", None)
            or "你是阿松, 桌面陪伴 AI, 活泼可爱、口语自然, 每次回复 1-2 句话, 不超过 50 字, 适合语音朗读。"
        )
        payload = {
            "type": "session.update",
            "event_id": uuid.uuid4().hex,
            "session": {
                "modalities": ["text", "audio"],
                "voice": self.voice,
                "instructions": instructions,
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm",
                "turn_detection": {
                    "type": self.turn_detection,
                    "threshold": 0.1,
                    "silence_duration_ms": 1500,
                },
            },
        }
        tools = self._collect_tools()
        if tools:
            payload["session"]["tools"] = tools
        await self._send(payload)
        logger.bind(tag=TAG).info(
            f"Qwen session.update: model={self.model}, voice={self.voice}, "
            f"vad={self.turn_detection}, tools={len(tools)}, instructions_len={len(instructions)}"
        )

    def _collect_tools(self):
        if not self.tools_enabled or self.conn is None:
            return []
        handler = getattr(self.conn, "func_handler", None)
        if handler is None:
            return []
        try:
            return list(handler.get_functions())
        except Exception as e:
            logger.bind(tag=TAG).warning(f"获取工具列表失败: {e}")
            return []

    async def _refresh_tools_later(self):
        try:
            await asyncio.sleep(8)
            if self._closing or not self._ws_ok():
                return
            tools = self._collect_tools()
            if tools:
                await self._send_session_update()
                logger.bind(tag=TAG).info(f"Qwen-Realtime 工具已注入: {len(tools)} 个")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.bind(tag=TAG).debug(f"延迟注入工具失败: {e}")

    # ---- 下行消息处理 ----
    async def _handle_message(self, data: dict):
        msg_type = data.get("type")
        if msg_type in (
            "session.created",
            "session.updated",
            "heartbeat",
            "conversation.created",
        ):
            return
        if msg_type == "error":
            err = data.get("error") or {}
            logger.bind(tag=TAG).error(f"Qwen error: {err.get('code')} {err.get('message')}")
            return
        if msg_type == "response.created":
            self._response_in_progress = True
            self._transcript = ""
            self._tool_queue = []
            self._emotion_done = False
            self._resampler.reset()
            self._audio_delta_count = 0
            self._opus_packets = 0
            self._user_transcript = ""
            logger.bind(tag=TAG).info("Qwen response.created")
            if self.conn and self.conn.tts:
                # 必须先通知设备进入播放状态, 否则设备仍在聆听态, 音频不发声
                try:
                    from core.handle.sendAudioHandle import send_tts_message

                    await send_tts_message(self.conn, "start")
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"发送 tts start 失败: {e}")
                self.conn.tts.tts_audio_queue.put(
                    (SentenceType.FIRST, None, "", self._sentence_id)
                )
            return
        if msg_type == "response.audio.delta":
            delta = data.get("delta")
            if not delta:
                return
            self._audio_delta_count += 1
            pcm = base64.b64decode(delta)
            out = self._resampler.feed(pcm)
            if out:
                self._push_opus(out)
            return
        if msg_type == "response.audio_transcript.delta":
            delta = data.get("delta") or ""
            if delta:
                self._transcript += delta
                now = time.time()
                if now - self._last_assistant_display >= 0.35:
                    self._last_assistant_display = now
                    await self._display_assistant_text(self._transcript)
                if not self._emotion_done and self.conn:
                    self._emotion_done = True
                    try:
                        from core.utils import textUtils

                        asyncio.run_coroutine_threadsafe(
                            textUtils.get_emotion(self, self._transcript[:30]),
                            self.conn.loop,
                        )
                    except Exception:
                        pass
            return
        if msg_type == "conversation.item.input_audio_transcription.delta":
            delta = data.get("delta") or ""
            if delta:
                self._user_transcript += delta
                now = time.time()
                if now - self._last_stt_display >= 0.4:
                    self._last_stt_display = now
                    await self._display_user_text(self._user_transcript)
            return
        if msg_type == "conversation.item.input_audio_transcription.completed":
            transcript = data.get("transcript") or ""
            if transcript:
                self._user_transcript = transcript
                await self._display_user_text(transcript)
                logger.bind(tag=TAG).info(f"Qwen 用户转写: {transcript}")
            return
        if msg_type == "response.function_call_arguments.done":
            self._tool_queue.append(
                {
                    "name": data.get("name"),
                    "arguments": data.get("arguments") or "{}",
                    "call_id": data.get("call_id") or data.get("event_id"),
                }
            )
            return
        if msg_type == "response.done":
            await self._finish_response()
            return
        if msg_type == "input_audio_buffer.speech_started":
            logger.bind(tag=TAG).info("Qwen speech_started")
            return
        if msg_type in (
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "input_audio_buffer.cleared",
            "conversation.item.created",
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
            "response.audio.done",
            "response.audio_transcript.done",
            "response.text.delta",
            "response.text.done",
            "response.function_call_arguments.delta",
        ):
            return
        logger.bind(tag=TAG).debug(f"Qwen 未处理事件: {msg_type}")

    async def _display_user_text(self, text: str):
        """把用户语音转写显示到机器人屏幕 ({"type":"stt"})。"""
        if not self.conn or not text:
            return
        try:
            from core.handle.sendAudioHandle import send_display_message

            await send_display_message(self.conn, text)
        except Exception as e:
            logger.bind(tag=TAG).debug(f"发送转写显示失败: {e}")

    async def _display_assistant_text(self, text: str):
        """把助手回复文本实时显示到机器人屏幕 (tts sentence_start)。"""
        if not self.conn or not text:
            return
        try:
            from core.handle.sendAudioHandle import send_tts_message

            await send_tts_message(self.conn, "sentence_start", text)
        except Exception as e:
            logger.bind(tag=TAG).debug(f"发送回复文本显示失败: {e}")

    def _push_opus(self, pcm16: bytes):
        if not self.conn or not self.conn.tts:
            return
        n = len(pcm16)
        frame_bytes = OPUS_FRAME * 2
        for i in range(0, n - n % frame_bytes, frame_bytes):
            try:
                packet = self._opus.encode(pcm16[i : i + frame_bytes], OPUS_FRAME)
            except Exception as e:
                logger.bind(tag=TAG).debug(f"opus 编码失败: {e}")
                continue
            self._opus_packets += 1
            self.conn.tts.tts_audio_queue.put(
                (SentenceType.MIDDLE, packet, self._transcript, self._sentence_id)
            )

    async def _finish_response(self):
        self._response_in_progress = False
        logger.bind(tag=TAG).info(
            f"Qwen response.done: audio_delta={self._audio_delta_count}, "
            f"opus_packets={self._opus_packets}, tools={len(self._tool_queue)}, "
            f"transcript_len={len(self._transcript)}"
        )
        if self._tool_queue:
            await self._run_tools()
            return
        if self.conn and self.conn.tts:
            text = self._transcript or ""
            if text:
                await self._display_assistant_text(text)
            self.conn.tts.tts_audio_queue.put(
                (SentenceType.LAST, [], text, self._sentence_id)
            )
        self._resampler.reset()
        self._transcript = ""

    async def _run_tools(self):
        if not self.conn or not self._tool_queue:
            return
        logger.bind(tag=TAG).info(
            f"Qwen 执行工具调用: {[c.get('name') for c in self._tool_queue]}"
        )
        outputs = []
        for call in self._tool_queue:
            name = call.get("name")
            args = call.get("arguments") or "{}"
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {"raw": args}
            try:
                result = await self.conn.func_handler.handle_llm_function_call(
                    self.conn, {"name": name, "arguments": args}
                )
                outputs.append(
                    {
                        "call_id": call.get("call_id"),
                        "output": getattr(result, "response", str(result)),
                    }
                )
            except Exception as e:
                logger.bind(tag=TAG).error(f"工具 {name} 执行失败: {e}")
                outputs.append({"call_id": call.get("call_id"), "output": f"error: {e}"})
        self._tool_queue = []
        if not self._ws_ok():
            return
        for o in outputs:
            try:
                await self._send(
                    {
                        "type": "conversation.item.create",
                        "event_id": uuid.uuid4().hex,
                        "item": {
                            "type": "function_call_output",
                            "call_id": o["call_id"],
                            "output": o["output"],
                        },
                    }
                )
            except Exception as e:
                logger.bind(tag=TAG).warning(f"回传工具结果失败: {e}")
                return
        try:
            await self._send(
                {
                    "type": "response.create",
                    "event_id": uuid.uuid4().hex,
                    "client_timestamp": int(time.time() * 1000),
                }
            )
        except Exception as e:
            logger.bind(tag=TAG).warning(f"触发续答失败: {e}")
