import time
import uuid
import asyncio
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from core.utils.dialogue import Message
from core.providers.asr.dto.dto import InterfaceType
from core.handle.receiveAudioHandle import startToChat
from core.handle.reportHandle import enqueue_asr_report
from core.handle.sendAudioHandle import send_stt_message, send_tts_message
from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType
from core.utils.util import remove_punctuation_and_length
from core.providers.tts.dto.dto import ContentType, TTSMessageDTO, SentenceType


TAG = __name__


class ListenTextMessageHandler(TextMessageHandler):
    """Listen消息处理器"""

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.LISTEN

    async def handle(self, conn: "ConnectionHandler", msg_json: Dict[str, Any]) -> None:
        if "mode" in msg_json:
            conn.client_listen_mode = msg_json["mode"]
            conn.logger.bind(tag=TAG).debug(
                f"客户端拾音模式：{conn.client_listen_mode}"
            )
        if msg_json["state"] == "start":
            # 设备从播放模式切回录音模式,清除所有音频状态和缓冲区
            conn.reset_audio_states()
            # Phase 7.1: ASR 预连接 - 在用户说话前建立 NLS 握手, 消除 ~2s 连接窗口丢字;
            # 5s 空转自动销毁 + 防重入锁 在 aliyunbl_stream.prewarm 内部处理
            if conn.asr is not None and hasattr(conn.asr, "prewarm"):
                try:
                    asyncio.create_task(conn.asr.prewarm(conn))
                except Exception as e:
                    conn.logger.bind(tag=TAG).warning(f"ASR 预连接调度失败: {e}")
            # Phase 7.1: EdgeTTS 预热 - 把微软云握手移出"说完话→出声"关键路径
            if conn.tts is not None and hasattr(conn.tts, "warmup"):
                try:
                    conn.tts.warmup()
                except Exception as e:
                    conn.logger.bind(tag=TAG).debug(f"EdgeTTS 预热调度失败: {e}")
            # GLM-Realtime: 新一轮聆听前清空实时输入缓冲
            if getattr(conn.llm, "is_realtime", False) and hasattr(conn.llm, "clear_buffer"):
                await conn.llm.clear_buffer()
        elif msg_json["state"] == "stop":
            # 收到stop但asr未初始化，跳过处理
            if conn.asr is None:
                return

            conn.client_voice_stop = True
            # GLM-Realtime (client_vad): 设备 VAD 判停 -> 提交音频并触发回复
            if getattr(conn.llm, "is_realtime", False) and hasattr(conn.llm, "commit_and_respond"):
                await conn.llm.commit_and_respond()
                return
            if conn.asr.interface_type == InterfaceType.STREAM:
                # 流式模式下，发送结束请求
                asyncio.create_task(conn.asr._send_stop_request())
            else:
                # 非流式模式：直接触发ASR识别
                if len(conn.asr_audio) > 0:
                    asr_audio_task = conn.asr_audio.copy()
                    conn.reset_audio_states()

                    if len(asr_audio_task) > 0:
                        await conn.asr.handle_voice_stop(conn, asr_audio_task)
        elif msg_json["state"] == "detect":
            conn.client_have_voice = False
            conn.reset_audio_states()
            # GLM-Realtime: 唤醒后清空缓冲, 开启新一轮
            if getattr(conn.llm, "is_realtime", False) and hasattr(conn.llm, "clear_buffer"):
                await conn.llm.clear_buffer()
            if "text" in msg_json:
                conn.last_activity_time = time.time() * 1000
                original_text = msg_json["text"]  # 保留原始文本
                filtered_len, filtered_text = remove_punctuation_and_length(
                    original_text
                )

                # 检查是否是设备呼叫指令 [device_call]
                if original_text.startswith("[device_call]"):
                    # 提取 tag 后的文本
                    call_text = original_text[len("[device_call]"):].strip()
                    conn.logger.bind(tag=TAG).info(f"收到设备呼叫指令: {call_text}")

                    # 标记为来电接听模式
                    conn.incoming_call = True

                    # 准备开始新会话
                    conn.sentence_id = uuid.uuid4().hex

                    await send_stt_message(conn, call_text)

                    # 等待tts初始化，最多等待3秒
                    start_time = time.time()
                    while time.time() - start_time < 3:
                        if conn.tts:
                            break
                        await asyncio.sleep(0.1)

                    if conn.tts:
                        conn.tts.store_tts_text(conn.sentence_id, call_text)
                        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=conn.sentence_id, sentence_type=SentenceType.FIRST, content_type=ContentType.ACTION))
                        conn.tts.tts_one_sentence(conn, ContentType.TEXT, content_detail=call_text)
                        conn.tts.tts_text_queue.put(TTSMessageDTO(sentence_id=conn.sentence_id, sentence_type=SentenceType.LAST, content_type=ContentType.ACTION))

                    # 添加到对话历史，让模型理解上下文
                    conn.dialogue.put(Message(role="assistant", content=call_text))
                    return

                # 唤醒/手势检测: 一律只进入聆听, 不把 detect 文本发给 LLM。
                # (阿松/你好小智是唤醒词; 摇一摇/托脸摇晃等是触摸手势文本,
                #  发给 LLM 会触发抢答/误聊, 用户接下来会说真正的话)
                conn.just_woken_up = True
                enqueue_asr_report(conn, original_text, [])
