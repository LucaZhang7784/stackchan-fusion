# Phase 7 实施: Qwen-Audio-3.0-Realtime 实时语音 (P7-1)

> 2026-08-05 · 自建链路 stackchan.selfhost

## 一、方案选型

国产实时语音 API 调研结论 (2026-08):

| 候选 | 结论 |
|---|---|
| DeepSeek / Kimi K3 / MiMo | ❌ 无官方 hosted 实时语音 API |
| GLM-Realtime (智谱) | ❌ 已实测: server VAD 判停不可靠 (aiohttp 客户端问题 + 判停缺陷), **已弃用** |
| **Qwen-Audio-3.0-Realtime (阿里百炼)** | ✅ 官方 WebSocket 实时语音, **语义 VAD (smart_turn)** 自动判停, 支持 Function Calling; 复用现有 DashScope key |
| 豆包实时语音 3.0 | 能力最强但需邀测, 暂缓 |
| 讯飞超拟人 | 延迟 <0.5s, 传统 SDK 风格, 备选 |

## 二、实现内容 (服务端, 固件无需改)

新 provider: `server-patch/core/providers/llm/qwen_audio_realtime/qwen_audio_realtime.py`

链路: 设备 opus(16k) → 服务端解码 PCM → `input_audio_buffer.append`(裸 PCM base64, 100ms/块)
→ Qwen smart_turn 语义 VAD (自动判停) → `response.audio.delta`(24k PCM)
→ 重采样 16k → opus → 设备播放。

- **唤醒语义保留**: 设备唤醒后流音频, smart_turn 自动判停回复; 空闲不耗流量。
- **打断**: 双击 → 设备 abort → `response.cancel` + 清缓冲。
- **屏幕双向显示**: 用户转写 (`conversation.item.input_audio_transcription.*` → `stt` 消息)
  与回复文本 (`response.audio_transcript.delta` → `tts sentence_start` 消息) 实时上屏。
- **工具调用**: 网关 MCP 工具注入 session, `function_call_arguments.done` → 本地执行 →
  `conversation.item.create(function_call_output, call_id)` → `response.create` 续答。
- **播报前发 `tts start`**: 设备必须先进入播放态, 否则音频不发声 (fusion_push 同款处理)。

## 三、关键排障记录 (都是真坑)

1. **aiohttp 客户端导致 VAD 失效**: 用 aiohttp 连实时 API, 服务端只回 session 事件、
   音频帧完全不触发 VAD; 换官方 demo 同款 `websockets` 库后立即正常。
   **必须用 websockets 库。**
2. **websockets 14 无 `.closed` 属性**: `ClientConnection` 没有 `.closed`,
   用 `close_code is None` 判断连接状态。
3. **播报无声**: 实时链路漏发 `{"type":"tts","state":"start"}`, 设备停在聆听态;
   在 `response.created` 时先发 tts start 再推音频。
4. **连接每 60-90s 被断**: Funnel 代理 aiohttp `heartbeat=30` 对不回 pong 的 ESP32
   强制断开 (heartbeat*2=60s) → 改为设备侧 `heartbeat=None`, 保活由后端 ping 承担。

## 四、如何启用

```yaml
# server/data/.config.yaml
selected_module:
  LLM: QwenAudioRealtimeLLM
LLM:
  QwenAudioRealtimeLLM:
    type: qwen_audio_realtime
    api_key: <百炼 DashScope key>   # 与 ASR/VLLM 同 key
    model: qwen-audio-3.0-realtime-flash   # 或 -plus
    voice: longanqian
    turn_detection: smart_turn
    tools: true
```

重建容器: `docker compose -f server/docker-compose.yml -f server-patch/docker-compose.fusion.yml up -d --force-recreate xiaozhi-esp32-server`

回退: `LLM` 改回 `DeepSeekLLM` 重建容器, 无需刷固件。

## 五、已知限制

| 限制 | 说明 |
|---|---|
| 音色为普通话系 | 百炼实时音色 (longanqian 等), 无粤语音色, 不跟随 EdgeTTS 粤语预设 |
| 按量计费 | flash: 输入音频 ¥30/百万token, 输出音频 ¥100/百万token (≈0.4-0.5 元/分钟) |
| 上下文 40K | 实时模式记忆由模型维护 |
| 设备连接依赖 Funnel | 断连会销毁实时会话, 已通过代理心跳修复; 长期建议国内中转 |

## 六、测试脚本

- `scripts/qwen_realtime_smoke.py`: 裸协议验证 (需 --key/--wav)
- `scripts/qwen_ws_test.py`: websockets 库端到端验证 (推荐)
