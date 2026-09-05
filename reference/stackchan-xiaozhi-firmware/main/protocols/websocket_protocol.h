#ifndef _WEBSOCKET_PROTOCOL_H_
#define _WEBSOCKET_PROTOCOL_H_


#include "protocol.h"

#include <web_socket.h>
#include <mutex>
#include <thread>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>

#define WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT (1 << 0)

class WebsocketProtocol : public Protocol {
public:
    WebsocketProtocol();
    ~WebsocketProtocol();

    bool Start() override;
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    bool OpenAudioChannel(bool silent = false) override;
    void CloseAudioChannel(bool send_goodbye = true) override;
    bool IsAudioChannelOpened() const override;
    bool IsStale(int max_idle_s) const override;

private:
    EventGroupHandle_t event_group_handle_;
    std::unique_ptr<WebSocket> websocket_;
    std::mutex connect_mutex_;
    std::thread keepalive_thread_;
    bool keepalive_running_ = false;
    int version_ = 1;

    void ParseServerHello(const cJSON* root);
    void StartKeepalive();
    void StopKeepalive();
    bool SendText(const std::string& text) override;
    std::string GetHelloMessage();
};

#endif
