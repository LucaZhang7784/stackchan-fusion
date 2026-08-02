# 固件侧: v2 真·主动播报 / 远程唤醒 (可选, 不需要可跳过)

## 背景
v1 融合(网关 + SERVER_MCP) 已覆盖:
- 机器人 -> agent: 语音说「让Codex/Claude…」, LLM 调 codex_query/claude_query, 结果语音播报
- agent -> 机器人: robot_say 排队, 机器人下次唤醒由 LLM 播报 (v1)

v2 的目标是「机器人空闲时也能被服务器主动唤醒并说话」, 即真·主动推送。

## 现状核查 (merge-v226 固件 2.2.6 源码)
`main/application.cc` 的 incoming JSON 处理器只识别:
  tts / stt / llm / mcp / system(reboot) / alert / custom(需 CONFIG_RECEIVE_CUSTOM_MESSAGE)
- alert: 播放震动提示音 + 显示消息, 但**不 TTS 朗读文字**
- custom: 仅显示到屏幕
=> 没有开箱即用的「服务端主动推一句话->设备朗读」。这是主动播报缺口的根源。

## 三条可行路线 (按推荐度)

### 路线 A (推荐, 改动最小): 服务器侧 TTS 推送补丁
服务器对已连接设备复用现有 TTS 管线(sendAudioMessage + EdgeTTS), 暴露一个 HTTP 接口
`POST /api/push {"mac": "...", "text": "..."}` -> 生成音频 -> 向该设备 WS 发 tts 帧。
设备侧**不需要改固件**(它本来就处理 tts 类型帧)。
注意: 设备空闲时音频通道是否打开需实测; 若空闲通道关闭, 需先复用 hello 打开通道(参考 server 源码 core/handle/sendAudioHandle.py)。
实现位置: xiaozhi-server core/http_server.py 加路由 + core/connection.py 加 send 方法。
风险: 修改的是容器内代码, 需把改动文件挂载进容器(server/docker-compose.yml volumes 增加 ./fusion-patch:/opt/... 覆盖)。

### 路线 B: 固件加 custom/alert 处理 -> TTS
在 merge-v226 `application.cc` 的 custom 分支(或新增 notify 分支)里, 收到 payload 后把 text 送入 TTS
(参考官方 device-call-guide.md 的 RemoteWakeup 思路, 但用文本->本地 TTS 组件)。
需要重新编译烧录固件; 机器人当前在 M5Stack 官方固件上, 要刷回自定义固件才能用。

### 路线 C (官方路径, 本网络受限): MQTT 网关 + RemoteWakeup
官方 device-call-guide.md: 智控台 -> MQTT网关 -> 设备远程唤醒(固件加 self.remote_wakeup 工具)。
**在本环境受限**: 设备连 MQTT broker(1883 TCP / 8884 UDP)是入站到主机,
AP 隔离 + Tailscale Funnel 只支持 443/8443/10000 且不能 UDP, 设备无法入站连到宿主机的 broker;
除非 broker 部署在公网 VPS 或改用 TLS 443, 否则这条路径不可行。

## RemoteWakeup 工具补丁参考代码 (路线 C 固件侧, 来自官方 device-call-guide)
application.h:
    void RemoteWakeup(const std::string& reason);
application.cc:
    void Application::RemoteWakeup(const std::string& reason){
        if (!protocol_) return;
        auto state = GetDeviceState();
        if (state == kDeviceStateIdle) {
            audio_service_.EncodeWakeWord();
            if (!protocol_->IsAudioChannelOpened()) {
                SetDeviceState(kDeviceStateConnecting);
                if (!protocol_->OpenAudioChannel()) {
                    audio_service_.EnableWakeWordDetection(true);
                    return;
                }
            }
            std::string wake_word = reason;
    #if CONFIG_USE_AFE_WAKE_WORD || CONFIG_USE_CUSTOM_WAKE_WORD
            while (auto packet = audio_service_.PopWakeWordPacket()) {
                protocol_->SendAudio(std::move(packet));
            }
            protocol_->SendWakeWordDetected(wake_word);
            SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
    #else
            play_popup_on_listening_ = true;
            SetListeningMode(aec_mode_ == kAecOff ? kListeningModeAutoStop : kListeningModeRealtime);
    #endif
        } else if (state == kDeviceStateSpeaking) {
            AbortSpeaking(kAbortReasonWakeWordDetected);
            SetDeviceState(kDeviceStateIdle);
        } else if (state == kDeviceStateActivating) {
            SetDeviceState(kDeviceStateIdle);
        }
    }
mcp_server.cc 增加工具:
    AddUserOnlyTool("self.remote_wakeup", "Remote wakeup function with configurable parameters",
        PropertyList({ Property("reason", kPropertyTypeString, "Wakeup reason") }),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string reason = properties["reason"].value<std::string>();
            Application::GetInstance().RemoteWakeup(reason);
            return true;
        });
编译要求: ESP32-S3、开 AEC、固件 2.1.0-2.2.6。

## 结论
v1 先跑通(零固件改动)。v2 优先做路线 A(服务器补丁), 路线 B 作为固件侧兜底, 路线 C 需要公网 MQTT 暂缓。