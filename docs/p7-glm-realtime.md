# Phase 7 实施: 实时语音 (P7-1) + 链路稳定性 (P7-2)

> 2026-08-05 · 自建链路 stackchan.selfhost

## 一、P7-1 选型结论 (为什么是 GLM-Realtime)

调研 2026-08 各家实时语音 API 现状:

| 候选 | 结论 |
|---|---|
| DeepSeek | ❌ 无官方 realtime 语音 API (只有 chat + 第三方 TTS/ASR 拼接) |
| MiMo (小米) | ❌ mimo.mi.com 只有流式 TTS 与音频理解, 无 realtime 对话 API |
| Kimi K3 (Moonshot) | ❌ OpenAI 兼容 chat API, 无 realtime 语音 |
| **GLM-Realtime (智谱)** | ✅ 官方 WebSocket 实时音视频 API, 支持 server VAD、打断、function calling; 价格 0.18 元/分钟 (flash) |

**结论: 用 GLM-Realtime。** 协议与 OpenAI Realtime 同构, 官方 Python/TS SDK
(MetaGLM/glm-realtime-sdk) 已 clone 到 `reference/glm-realtime-sdk` 作参考。

## 二、P7-1 实现内容 (服务端, 固件无需改)

新 provider: `server-patch/core/providers/llm/glm_realtime/glm_realtime.py`

链路: 设备 opus(16k) → 服务端解码 PCM → `input_audio_buffer.append`
→ GLM-Realtime (server VAD 自动断句/打断) → `response.audio.delta` (24k PCM)
→ 重采样 16k → opus → 设备播放。

- **保留唤醒语义**: 设备唤醒(双击/阿松/摸头)后才流音频, 实时会话在唤醒会话内
  自动应答; 空闲不耗音频流量。
- **打断**: 双击 → 设备 abort → 服务端 `response.cancel` + 清缓冲。
- **主动播报兼容**: fusion_push 推送前自动打断实时会话, 播完恢复 sentence_id。
- **工具调用**: 网关 MCP 工具 (agent_status/agent_query 等) 注入 GLM
  session.update, `response.function_call_arguments.done` → 本地执行 →
  `conversation.item.create(function_call_output)` → `response.create` 续答。
- **表情联动**: 首个转写分片触发 `textUtils.get_emotion`。

## 三、如何启用

1. 到 https://bigmodel.cn 申请 API Key (实时语音需实名+开通 glm-realtime)。
2. 编辑 `server/data/.config.yaml`:
   ```yaml
   LLM:
     GLMRealtimeLLM:
       type: glm_realtime
       api_key: "<你的智谱key>"
       model: glm-realtime-flash   # 或 glm-realtime-air
       voice: tongtong
   selected_module:
     LLM: GLMRealtimeLLM
   ```
3. `docker compose ... up -d xiaozhi-esp32-server` (重建容器加载新挂载)。
4. 冒烟测试:
   `docker exec xiaozhi-esp32-server python /opt/xiaozhi-esp32-server/tests/glm_realtime_smoke.py --key <key> --wav test.pcm`
5. 回退: `selected_module.LLM` 改回 `DeepSeekLLM` 重建容器即可, 无需刷固件。

## 四、已知限制 (如实说明)

| 限制 | 说明 |
|---|---|
| 音色为普通话系 | GLM 内置音色 (tongtong/xiaochen/female-tianmei 等), **无粤语音色**, 不跟随 EdgeTTS 粤语预设 |
| 需要智谱 key + 按分钟计费 | flash 0.18 元/分钟, air 0.3 元/分钟; 无免费额度替代时保留 DeepSeek 链路 |
| 上下文 8K (~20 轮) | 实时模式记忆由 GLM 维护, 不用本机 memory 插件 |
| 实时模式不走 ASR/TTS 上报 | 队列/字幕仍可用 (转写文本), 但 ASR 统计口径不同 |
| 工具注入延迟 | MCP 工具初始化可能晚于首帧音频, 8s 后自动补发 session.update |

## 五、P7-2 链路稳定性改动

| 改动 | 位置 | 效果 |
|---|---|---|
| 服务端 WS ping 显式化 | `server-patch/core/websocket_server.py` (ping_interval=15s) | 及时探测 Funnel 半开连接 |
| 应答设备应用层 ping | `server/data/.config.yaml` `enable_websocket_ping: true` | 保活 + 重置空闲计时 |
| 固件 30s 应用层 ping | `reference/.../websocket_protocol.cc` (待编译) | 文本帧可穿透 funnel proxy, 设备侧主动保活 |
| 代理心跳 | `server/funnel_proxy.py` (aiohttp heartbeat) | 双端保活 |
| GLM 实时会话自动重连 | provider 内指数退避 (2s→15s, 最多 5 次) | Funnel 抖动自愈 |

> Funnel 本身是公网中继, 8-21s 握手/随机断连属 Tailscale 基础设施抖动;
> P7-2 的保活+重连只能缩短"无感知窗口", 无法消除。长期备线方案:
> 同 tailnet 直连 (机器人需能路由 tailnet IP) 或 cloudflared 快速隧道
> (需改设备 NVS 里的 websocket url, 一般要重新配网/OTA)。
