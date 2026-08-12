import json
import uuid
import asyncio
import time
import websockets
from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

from config.logger import setup_logging
from core.providers.asr.base import ASRProviderBase
from core.providers.asr.dto.dto import InterfaceType

TAG = __name__
logger = setup_logging()


class ASRProvider(ASRProviderBase):
    def __init__(self, config, delete_audio_file):
        super().__init__()
        self.interface_type = InterfaceType.STREAM
        self.config = config
        self.text = ""
        self.asr_ws = None
        self.forward_task = None
        self.is_processing = False
        self.server_ready = False  # 服务器准备状态
        self.task_id = None  # 当前任务ID
        # Phase 7.1: ASR 预连接(防重入锁 / 5s 空转看门狗 / 语音标记)
        self._prewarm_lock = None
        self._watchdog_task = None
        self._voice_seen = False
        self._cleanup_lock = None  # Phase 7.1: 幂等清理锁, 防 watchdog 与 forward loop 双清理竞态

        # 阿里百炼配置
        self.api_key = config.get("api_key")
        self.model = config.get("model", "paraformer-realtime-v2")
        self.sample_rate = config.get("sample_rate", 16000)
        self.format = config.get("format", "pcm")

        # 可选参数
        self.vocabulary_id = config.get("vocabulary_id")
        self.disfluency_removal_enabled = config.get("disfluency_removal_enabled", False)
        self.language_hints = config.get("language_hints")
        self.semantic_punctuation_enabled = config.get("semantic_punctuation_enabled", False)
        # Phase 7.1 终极修补: 同音词纠错映射(wrong|right), 在 ASR 返回前替换,
        # 解决「可头大/扣代码/扣德斯」等被识别成非目标词导致不触发 agent_query 的问题
        self.correct_words = {}
        for item in config.get("correct_words", []) or []:
            if isinstance(item, str) and "|" in item:
                wrong, right = item.split("|", 1)
                wrong, right = wrong.strip(), right.strip()
                if wrong:
                    self.correct_words[wrong] = right
        max_sentence_silence = config.get("max_sentence_silence")
        self.max_sentence_silence = int(max_sentence_silence) if max_sentence_silence else 200
        self.multi_threshold_mode_enabled = config.get("multi_threshold_mode_enabled", False)
        self.punctuation_prediction_enabled = config.get("punctuation_prediction_enabled", True)
        self.inverse_text_normalization_enabled = config.get("inverse_text_normalization_enabled", True)

        # WebSocket URL
        self.ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

        self.output_dir = config.get("output_dir", "./audio_output")
        self.delete_audio_file = delete_audio_file

    async def open_audio_channels(self, conn):
        await super().open_audio_channels(conn)

    async def receive_audio(self, conn, pcm_frame, audio_have_voice):
        # 先调用父类方法处理基础逻辑
        await super().receive_audio(conn, pcm_frame, audio_have_voice)

        if audio_have_voice:
            self._voice_seen = True

        # 只在有声音且没有连接时建立连接
        if audio_have_voice and not self.is_processing and not self.asr_ws:
            try:
                # 后台建立 DashScope 连接: 不能阻塞设备连接的消息处理循环,
                # 否则 DashScope 握手慢/卡时会拖死设备连接(「聆听后 ~12s 断连」的根因)
                asyncio.create_task(self._start_recognition(conn))
            except Exception as e:
                logger.bind(tag=TAG).error(f"开始识别失败: {str(e)}")
                await self._cleanup()
                return

        # 发送音频数据
        if self.asr_ws and self.is_processing and self.server_ready:
            try:
                await self.asr_ws.send(pcm_frame)
            except Exception as e:
                logger.bind(tag=TAG).warning(f"发送音频失败: {str(e)}")
                await self._cleanup()

    async def prewarm(self, conn: "ConnectionHandler"):
        """listen:start 时预连接 NLS ASR, 在用户说话前完成握手(消除连接窗口丢字)"""
        if self._prewarm_lock is None:
            self._prewarm_lock = asyncio.Lock()
        async with self._prewarm_lock:
            # 防重入: 连续唤醒/双击时严禁重复拉起多个预连线程
            if self.is_processing or self.asr_ws is not None:
                return
            try:
                await self._start_recognition(conn)
            except Exception as e:
                logger.bind(tag=TAG).error(f"ASR 预连接失败: {str(e)}")
                return
            # 5s 空转自动销毁: 预连后用户 5s 内未说话 → 关闭 WebSocket, 防空挂/扣费
            if self._watchdog_task and not self._watchdog_task.done():
                self._watchdog_task.cancel()
            self._watchdog_task = asyncio.create_task(self._prewarm_watchdog(conn))

    async def _prewarm_watchdog(self, conn: "ConnectionHandler"):
        try:
            await asyncio.sleep(5)
            if not self._voice_seen:
                logger.bind(tag=TAG).info(
                    f"ASR 预连 5s 空转(用户未说话), 自动关闭 (task_id: {self.task_id})"
                )
                await self._cleanup()
                try:
                    # Phase 7.1: 通知设备终止聆听, 否则机器人卡在聆听态(LED 蓝灯不恢复)
                    await conn.websocket.send(
                        json.dumps({"type": "listen", "state": "stop"}, ensure_ascii=False)
                    )
                    logger.bind(tag=TAG).info("已下发 listen: stop 驱动设备回复待机状态(暖橙色灯)")
                except Exception as e:
                    logger.bind(tag=TAG).warning(f"下发 listen: stop 失败: {e}")
        except asyncio.CancelledError:
            pass

    async def _start_recognition(self, conn: "ConnectionHandler"):
        """开始识别会话"""
        try:
            # 如果为手动模式,设置超时时长为最大值
            if conn.client_listen_mode == "manual":
                # Phase 7.1: 手动(双击)模式句末静默 6000ms→800ms, 消除说完话干等 6 秒
                self.max_sentence_silence = 800

            self.is_processing = True
            self.task_id = uuid.uuid4().hex
            self._voice_seen = False
            self.asr_start_ts = time.monotonic()
            logger.bind(tag=TAG).info(f"ASR 会话开始 (task_id: {self.task_id})")

            # 建立WebSocket连接
            headers = {
                "Authorization": f"Bearer {self.api_key}"
            }

            logger.bind(tag=TAG).debug(f"正在连接阿里百炼ASR服务, task_id: {self.task_id}")

            # DashScope 握手加 5s 超时: 服务不可达时快速失败, 不拖死设备连接
            self.asr_ws = await asyncio.wait_for(
                websockets.connect(
                    self.ws_url,
                    additional_headers=headers,
                    max_size=1000000000,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=5,
                ),
                timeout=5,
            )

            logger.bind(tag=TAG).debug("WebSocket连接建立成功")

            self.server_ready = False
            self.forward_task = asyncio.create_task(self._forward_results(conn))

            # 发送run-task指令
            run_task_msg = self._build_run_task_message()
            await self.asr_ws.send(json.dumps(run_task_msg, ensure_ascii=False))
            logger.bind(tag=TAG).debug("已发送run-task指令，等待服务器准备...")

        except Exception as e:
            logger.bind(tag=TAG).error(f"建立ASR连接失败: {str(e)}")
            if self.asr_ws:
                try:
                    await self.asr_ws.close()
                except Exception:
                    pass
                self.asr_ws = None
            self.is_processing = False
            # 不 re-raise: 后台任务路径下未处理异常只会变成噪声日志,
            # 且设备连接必须存活(失败后由下一次语音重新建立 ASR)

    def _build_run_task_message(self) -> dict:
        """构建run-task指令"""
        message = {
            "header": {
                "action": "run-task",
                "task_id": self.task_id,
                "streaming": "duplex"
            },
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": self.model,
                "parameters": {
                    "format": self.format,
                    "sample_rate": self.sample_rate,
                    "disfluency_removal_enabled": self.disfluency_removal_enabled,
                    "semantic_punctuation_enabled": self.semantic_punctuation_enabled,
                    "max_sentence_silence": self.max_sentence_silence,
                    "multi_threshold_mode_enabled": self.multi_threshold_mode_enabled,
                    "punctuation_prediction_enabled": self.punctuation_prediction_enabled,
                    "inverse_text_normalization_enabled": self.inverse_text_normalization_enabled,
                },
                "input": {}
            }
        }

        # 只有当模型名称以v2结尾时才添加vocabulary_id参数
        if self.model.lower().endswith("v2"):
            message["payload"]["parameters"]["vocabulary_id"] = self.vocabulary_id

        if self.language_hints:
            message["payload"]["parameters"]["language_hints"] = self.language_hints

        # Phase 7.1: 打印实际发往阿里云 NLS 的 run-task JSON, 供核对判停参数确实透传
        # (streaming ASR 的句末静默参数为 payload.parameters.max_sentence_silence, 单位 ms)
        logger.bind(tag=TAG).info(f"ASR run-task JSON: {json.dumps(message, ensure_ascii=False)}")
        return message

    async def _forward_results(self, conn: "ConnectionHandler"):
        """转发识别结果"""
        try:
            while not conn.stop_event.is_set():
                # 获取当前连接的音频数据
                audio_data = conn.asr_audio
                try:
                    response = await asyncio.wait_for(self.asr_ws.recv(), timeout=1.0)
                    result = json.loads(response)

                    header = result.get("header", {})
                    payload = result.get("payload", {})
                    event = header.get("event", "")

                    # 处理task-started事件
                    if event == "task-started":
                        # Phase 7.1: 校验阿里云返回 status=20000000 Success。
                        # 注意: task-started 事件头通常不带 status_code(实测为 None),
                        # 只在"存在且非 20000000"时才判失败, 避免误杀正常会话。
                        status_code = header.get("status_code")
                        if status_code is not None and status_code != 20000000:
                            logger.bind(tag=TAG).error(
                                f"ASR task-started 非成功: status={status_code} "
                                f"error_code={header.get('error_code')} msg={header.get('error_message')} "
                                f"(task_id: {self.task_id})"
                            )
                            break
                        logger.bind(tag=TAG).debug(
                            f"task-started 事件头: {json.dumps(header, ensure_ascii=False)}"
                        )
                        self.server_ready = True
                        logger.bind(tag=TAG).debug("服务器已准备，开始发送缓存音频...")

                        # 发送全部缓存音频: 只发最后 10 帧(600ms)会丢掉用户
                        # 开头 1~2 秒的语音(「前几个字不全」的根因)
                        if conn.asr_audio:
                            for cached_pcm in conn.asr_audio:
                                try:
                                    await self.asr_ws.send(cached_pcm)
                                    # Phase 7.1: 预卷回溯逐帧让出事件循环, 防止大包异步解压阻塞主循环
                                    await asyncio.sleep(0)
                                except Exception as e:
                                    logger.bind(tag=TAG).warning(f"发送缓存音频失败: {e}")
                                    break
                            conn.asr_audio.clear()
                        continue

                    # 处理result-generated事件
                    elif event == "result-generated":
                        output = payload.get("output", {})
                        sentence = output.get("sentence", {})

                        text = sentence.get("text", "")
                        sentence_end = sentence.get("sentence_end", False)
                        end_time = sentence.get("end_time")

                        # 判断是否为最终结果(sentence_end为True且end_time不为null)
                        is_final = sentence_end and end_time is not None

                        if is_final:
                            logger.bind(tag=TAG).info(f"识别到文本: {text}")

                            # 手动模式下累积识别结果
                            if conn.client_listen_mode == "manual":
                                if self.text:
                                    self.text += text
                                else:
                                    self.text = text

                                # 手动模式也立即触发处理: 说完一句就回复,
                                # 不再等 stop 信号(否则双击/手动聆听的 ASR 结果会滞留 ~27s)
                                logger.bind(tag=TAG).debug("收到最终识别结果，触发处理")
                                await self.handle_voice_stop(conn, audio_data)
                                break
                            else:
                                # 自动模式下直接覆盖
                                self.text = text
                                elapsed = time.monotonic() - getattr(self, "asr_start_ts", time.monotonic())
                                logger.bind(tag=TAG).info(f"ASR 识别耗时 {elapsed:.2f}s 文本: {text}")
                                await self.handle_voice_stop(conn, audio_data)
                                break

                    # 处理task-finished事件
                    elif event == "task-finished":
                        logger.bind(tag=TAG).debug("任务已完成")
                        break

                    # 处理task-failed事件
                    elif event == "task-failed":
                        error_code = header.get("error_code", "UNKNOWN")
                        error_message = header.get("error_message", "未知错误")
                        logger.bind(tag=TAG).error(f"任务失败: {error_code} - {error_message}")
                        break

                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    logger.bind(tag=TAG).info("ASR服务连接已关闭")
                    self.is_processing = False
                    break
                except Exception as e:
                    logger.bind(tag=TAG).error(f"处理结果失败: {str(e)}")
                    break

        except Exception as e:
            logger.bind(tag=TAG).error(f"结果转发失败: {str(e)}")
        finally:
            # 清理连接的音频缓存
            await self._cleanup()
            conn.reset_audio_states()

    async def _send_stop_request(self):
        """发送停止请求(用于手动模式停止录音)"""
        if self.asr_ws:
            try:
                # 先停止音频发送
                self.is_processing = False

                logger.bind(tag=TAG).debug("收到停止请求，发送finish-task指令")
                await self._send_finish_task()
            except Exception as e:
                logger.bind(tag=TAG).error(f"发送停止请求失败: {e}")

    async def _send_finish_task(self):
        """发送finish-task指令"""
        if self.asr_ws and self.task_id:
            try:
                finish_msg = {
                    "header": {
                        "action": "finish-task",
                        "task_id": self.task_id,
                        "streaming": "duplex"
                    },
                    "payload": {
                        "input": {}
                    }
                }
                await self.asr_ws.send(json.dumps(finish_msg, ensure_ascii=False))
                logger.bind(tag=TAG).debug("已发送finish-task指令")
            except Exception as e:
                logger.bind(tag=TAG).error(f"发送finish-task指令失败: {e}")

    async def _cleanup(self):
        """清理资源"""
        if self._cleanup_lock is None:
            self._cleanup_lock = asyncio.Lock()
        async with self._cleanup_lock:
            await self._cleanup_locked()

    async def _cleanup_locked(self):
        """清理资源(调用方需持有 _cleanup_lock)"""
        if not self.is_processing and self.asr_ws is None and self.task_id is None:
            return  # 已清理过, 幂等返回

        logger.bind(tag=TAG).debug(f"开始ASR会话清理 | 当前状态: processing={self.is_processing}, server_ready={self.server_ready}")

        # 状态重置
        self.is_processing = False
        self.server_ready = False
        self._voice_seen = False
        logger.bind(tag=TAG).debug("ASR状态已重置")

        # 关闭连接
        if self.asr_ws:
            try:
                # 先发送finish-task指令
                await self._send_finish_task()
                # 等待一小段时间让服务器处理
                await asyncio.sleep(0.1)

                if not getattr(self.asr_ws, "closed", False):
                    logger.bind(tag=TAG).debug("正在关闭WebSocket连接")
                    await asyncio.wait_for(self.asr_ws.close(), timeout=2.0)
                    logger.bind(tag=TAG).debug("WebSocket连接已关闭")
            except websockets.ConnectionClosed:
                logger.bind(tag=TAG).debug("WebSocket连接在清理前已由服务端正常关闭")
            except Exception as e:
                logger.bind(tag=TAG).error(f"关闭WebSocket连接失败: {e}")
            finally:
                self.asr_ws = None

        # 清理任务引用
        self.forward_task = None
        self.task_id = None

        logger.bind(tag=TAG).debug("ASR会话清理完成")

    async def speech_to_text(self, opus_data, session_id, artifacts=None):
        """获取识别结果"""
        result = self.text
        self.text = ""
        if result and self.correct_words:
            for wrong, right in self.correct_words.items():
                if wrong in result:
                    result = result.replace(wrong, right)
                    logger.bind(tag=TAG).info(f"ASR 纠错: '{wrong}' -> '{right}' | 原文: {result}")
        return result, None

    async def close(self):
        """关闭资源"""
        await self._cleanup()
