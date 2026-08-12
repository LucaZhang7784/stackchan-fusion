import os
import asyncio
import uuid
import edge_tts
import opuslib_next
from datetime import datetime
from core.providers.tts.base import TTSProviderBase


MPEG1_BITRATE = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
MPEG2_BITRATE = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
MPEG_SAMPLERATE = [44100, 48000, 32000, 0]


def _extract_mp3_frame(buf: bytearray):
    """从缓冲取出一整帧 MP3(Layer III), 未收满返回 None; 非法同步字节丢弃。

    Phase 7.1: 防止 edge_tts 流式块把 MP3 帧切碎, 攒满完整帧才喂给 ffmpeg。
    """
    while len(buf) >= 4:
        if buf[0] != 0xFF or (buf[1] & 0xE0) != 0xE0:
            del buf[0]
            continue
        ver = (buf[1] >> 3) & 0x03          # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
        layer = (buf[1] >> 1) & 0x03        # 1=Layer III
        bitrate_idx = (buf[2] >> 4) & 0x0F
        sample_idx = (buf[2] >> 2) & 0x03
        padding = (buf[2] >> 1) & 0x01
        if layer != 1 or bitrate_idx in (0, 15) or sample_idx == 3:
            del buf[0]
            continue
        if ver == 3:
            bitrate = MPEG1_BITRATE[bitrate_idx] * 1000
            sample_rate = MPEG_SAMPLERATE[sample_idx]
            frame_len = 144 * bitrate // sample_rate + padding
        else:
            bitrate = MPEG2_BITRATE[bitrate_idx] * 1000
            sample_rate = MPEG_SAMPLERATE[sample_idx] // 2
            frame_len = 72 * bitrate // sample_rate + padding
        if frame_len <= 0:
            del buf[0]
            continue
        if len(buf) >= frame_len:
            frame = bytes(buf[:frame_len])
            del buf[:frame_len]
            return frame
        return None
    return None


class TTSProvider(TTSProviderBase):
    TTS_PARAM_CONFIG = [
        ("ttsVolume", "volume", 0, 100, 50, int),
        ("ttsRate", "speech_rate", -100, 100, 0, int),
        ("ttsPitch", "pitch_rate", -100, 100, 0, int),
    ]

    def __init__(self, config, delete_audio_file):
        super().__init__(config, delete_audio_file)
        if config.get("private_voice"):
            self.voice = config.get("private_voice")
        else:
            self.voice = config.get("voice")
        self.audio_file_type = config.get("format", "mp3")

        volume = config.get("volume", "50")
        self.volume = int(volume) if volume else 50

        speech_rate = config.get("rate", "0")
        self.speech_rate = int(speech_rate) if speech_rate else 0

        pitch_rate = config.get("pitch", "0")
        self.pitch_rate = int(pitch_rate) if pitch_rate else 0

        # 应用百分比调整
        self._apply_percentage_params(config)

        self.edge_rate = f"{self.speech_rate:+}%"
        self.edge_volume = f"{self.volume:+}%"
        self.edge_pitch = f"{self.pitch_rate:+}Hz"
        # Phase 7.1: 流式推流开关(默认开, 出错可回退)
        self.streaming_enabled = bool(config.get("streaming", True))
        self._warmup_task = None

    def warmup(self):
        """listen 开始时后台预热 EdgeTTS 连接(握手移出关键路径, 实测首包 2.3s→1.6s)。"""
        if self._warmup_task is not None and not self._warmup_task.done():
            return  # 已有预热在跑
        try:
            self._warmup_task = asyncio.create_task(self._warmup_async())
        except Exception:
            pass

    async def _warmup_async(self):
        try:
            communicate = edge_tts.Communicate(
                "嗯",
                voice=self.voice,
                rate=self.edge_rate,
                volume=self.edge_volume,
                pitch=self.edge_pitch,
            )
            # Phase 7.1 终极修补: asyncio.wait_for 不能包在 async for 上
            # (协程不可迭代 → RuntimeWarning "coroutine 'wait_for' was never awaited")。
            # 用内部协程包装异步迭代, 只消费首包完成握手, 5s 超时防挂死。
            async def _drain_first():
                async for _chunk in communicate.stream():
                    return  # 只取首个音频块即完成握手

            await asyncio.wait_for(_drain_first(), timeout=5)
            from config.logger import setup_logging
            setup_logging().bind(tag=__name__).info("EdgeTTS 预热完成")
        except Exception as e:
            from config.logger import setup_logging
            setup_logging().bind(tag=__name__).debug(f"EdgeTTS 预热失败(忽略): {e}")

    def stream_to_opus(self, text, sentence_id, on_frame):
        """边收 EdgeTTS MP3 边转 Opus 推流(同步入口, 内部 asyncio 流式)。

        Phase 7.1: 严禁等整句合成完才发; MP3 帧头对齐 + ffmpeg 管道实时解码,
        60ms@16k Opus 帧逐帧回调 on_frame。asyncio 解耦 stdin 写入与 stdout 读取,
        finally 强制 terminate 回收僵尸进程。
        """
        asyncio.run(self._stream_to_opus_async(text, sentence_id, on_frame))

    async def _stream_to_opus_async(self, text, sentence_id, on_frame):
        sample_rate = 16000
        frame_size = sample_rate * 60 // 1000  # 960 samples @60ms
        encoder = opuslib_next.Encoder(sample_rate, 1, opuslib_next.APPLICATION_AUDIO)
        # Phase 7.1 终极修补诊断: 统计实际产出的 Opus 帧数, 区分"没合成"vs"没发送"
        _frame_counter = {"n": 0}
        _orig_on_frame = on_frame

        def _counted(frame):
            _frame_counter["n"] += 1
            _orig_on_frame(frame)

        on_frame = _counted
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "mp3", "-i", "pipe:0",
            "-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def _reader():
            """并发读 stdout: PCM → 60ms 帧 → Opus → on_frame(与 stdin 写入解耦, 防管道死锁)"""
            pcm_buf = b""
            try:
                while True:
                    data = await proc.stdout.read(4096)
                    if not data:
                        break
                    pcm_buf += data
                    while len(pcm_buf) >= frame_size * 2:
                        frame = pcm_buf[:frame_size * 2]
                        pcm_buf = pcm_buf[frame_size * 2:]
                        on_frame(encoder.encode(frame, frame_size))
                if pcm_buf:  # 末帧补零
                    pcm_buf += b"\x00" * (frame_size * 2 - len(pcm_buf))
                    on_frame(encoder.encode(pcm_buf, frame_size))
            finally:
                try:
                    await proc.wait()
                except Exception:
                    pass

        reader_task = asyncio.create_task(_reader())
        mp3_buf = bytearray()
        try:
            communicate = edge_tts.Communicate(
                text,
                voice=self.voice,
                rate=self.edge_rate,
                volume=self.edge_volume,
                pitch=self.edge_pitch,
            )
            async for chunk in communicate.stream():
                if chunk["type"] != "audio":
                    continue
                mp3_buf.extend(chunk["data"])
                while True:
                    frame = _extract_mp3_frame(mp3_buf)
                    if frame is None:
                        break
                    proc.stdin.write(frame)
                    await proc.stdin.drain()
            # 剩余未对齐字节也喂给 ffmpeg(其内部自行同步, 不会因半帧报错)
            if mp3_buf:
                proc.stdin.write(bytes(mp3_buf))
                mp3_buf.clear()
                await proc.stdin.drain()
            proc.stdin.write_eof()
            await reader_task
            from config.logger import setup_logging
            setup_logging().bind(tag=__name__).info(
                f"stream_to_opus DONE: {_frame_counter['n']} frames | text='{text[:24]}' | sid={sentence_id}"
            )
        finally:
            # Phase 7.1: 强制回收子进程, 防僵尸
            if not reader_task.done():
                reader_task.cancel()
            try:
                if proc.returncode is None:
                    proc.terminate()
                await proc.wait()
            except Exception:
                pass

    def generate_filename(self, extension=".mp3"):
        return os.path.join(
            self.output_file,
            f"tts-{datetime.now().date()}@{uuid.uuid4().hex}{extension}",
        )

    async def text_to_speak(self, text, output_file):
        try:
            communicate = edge_tts.Communicate(
                text,
                voice=self.voice,
                rate=self.edge_rate,
                volume=self.edge_volume,
                pitch=self.edge_pitch,
            )
            if output_file:
                # 确保目录存在并创建空文件
                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                with open(output_file, "wb") as f:
                    pass

                # 流式写入音频数据
                with open(output_file, "ab") as f:  # 改为追加模式避免覆盖
                    async for chunk in communicate.stream():
                        if chunk["type"] == "audio":  # 只处理音频数据块
                            f.write(chunk["data"])
            else:
                # 返回音频二进制数据
                audio_bytes = b""
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        audio_bytes += chunk["data"]
                return audio_bytes
        except Exception as e:
            error_msg = f"Edge TTS请求失败: {e}"
            raise Exception(error_msg)  # 抛出异常，让调用方捕获
