#include "wifi_board.h"
#include "cores3_audio_codec.h"
#include "display/lcd_display.h"
#include "application.h"
#include "config.h"
#include "power_save_timer.h"
#include "i2c_device.h"
#include "axp2101.h"
#include "mcp_server.h"

#include <esp_log.h>
#include <esp_heap_caps.h>
#include <driver/i2c_master.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_ili9341.h>
#include <esp_timer.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include "esp_video.h"
#include "lvgl.h"
#include "SCSCL.h"
#include "i2c_bus.h"
#include "bmi270_api.h"
#include "bmi2.h"

// BMI270 SDK 在 .a 里有这些 public 符号但头文件未暴露——自己声明用来绕过 bmi270_sensor_create 硬编码 0x68 的限制
extern "C" {
    int8_t bmi270_init(struct bmi2_dev *dev);
    extern const uint8_t bmi270_config_file[];
}

// BMI270 自定义 I2C read/write（地址 0x69）
static int8_t Bmi270I2cRead(uint8_t reg_addr, uint8_t *data, uint32_t len, void *intf_ptr) {
    auto dev = (i2c_master_dev_handle_t)intf_ptr;
    return i2c_master_transmit_receive(dev, &reg_addr, 1, data, len, 200) == ESP_OK ? 0 : -1;
}

static int8_t Bmi270I2cWrite(uint8_t reg_addr, const uint8_t *data, uint32_t len, void *intf_ptr) {
    static uint8_t big_buf[8300];  // BMI270 config blob ~8KB
    if (len + 1 > sizeof(big_buf)) return -1;
    auto dev = (i2c_master_dev_handle_t)intf_ptr;
    big_buf[0] = reg_addr;
    memcpy(big_buf + 1, data, len);
    return i2c_master_transmit(dev, big_buf, len + 1, 500) == ESP_OK ? 0 : -1;
}

static void Bmi270DelayUs(uint32_t period_us, void *intf_ptr) {
    if (period_us < 1000) {
        esp_rom_delay_us(period_us);
    } else {
        vTaskDelay(pdMS_TO_TICKS((period_us + 999) / 1000));
    }
}

#define TAG "M5StackCoreS3Board"

class FaceTracker;

class StackChanServo {
public:
    bool Begin() {
        if (!bus_.begin(UART_NUM_1, 1000000, 6, 7)) {
            ESP_LOGE("Servo", "SCS bus begin failed");
            return false;
        }
        ESP_LOGI("Servo", "SCS bus init OK on UART1 GPIO6/7 @1Mbps");
        MoveTo(0, 30, 1500);

        esp_timer_create_args_t args = {};
        args.callback = &StackChanServo::IdleScanCb;
        args.arg = this;
        args.dispatch_method = ESP_TIMER_TASK;
        args.name = "servo_idle";
        args.skip_unhandled_events = true;
        esp_timer_create(&args, &idle_timer_);
        esp_timer_start_periodic(idle_timer_, 4000000);
        scan_running_ = true;
        return true;
    }

    void MoveTo(int yaw_deg, int pitch_deg, int time_ms) {
        if (yaw_deg < -45) yaw_deg = -45;
        if (yaw_deg > 45) yaw_deg = 45;
        if (pitch_deg < 5) pitch_deg = 5;
        if (pitch_deg > 60) pitch_deg = 60;
        int yaw_pos = 460 + yaw_deg * 16 / 5;
        int pitch_pos = 620 + pitch_deg * 16 / 5;
        ESP_LOGI("Servo", "MoveTo yaw=%d pitch=%d t=%d (caller=%p)",
                 yaw_deg, pitch_deg, time_ms, __builtin_return_address(0));
        bus_.WritePos(1, yaw_pos, time_ms, 0);
        bus_.WritePos(2, pitch_pos, time_ms, 0);
        // 记当前命令位置——Nod/Shake 动画用这个作 base，而不是 tracker_->GetYaw()
        // 后者只反映人脸追踪目标，不反映 MCP head.move 设的硬位置。
        last_yaw_deg_ = yaw_deg;
        last_pitch_deg_ = pitch_deg;
    }

    void PauseScan() {
        if (scan_running_ && idle_timer_) {
            esp_timer_stop(idle_timer_);
            scan_running_ = false;
        }
    }

    void ResumeScan() {
        if (!scan_running_ && idle_timer_) {
            esp_timer_start_periodic(idle_timer_, 4000000);
            scan_running_ = true;
        }
    }

    void Center() { MoveTo(0, 30, 600); }

    void SetFaceTracker(FaceTracker* ft) { tracker_ = ft; }

    // 当前命令位置（最后一次 MoveTo 设的）— Nod/Shake 用这个作 base
    int GetCurrentYaw() const { return last_yaw_deg_; }
    int GetCurrentPitch() const { return last_pitch_deg_; }

    void Nod();
    void Shake();
    void Tilt();

    bool IsAnimating() const { return anim_running_; }

private:
    static void IdleScanCb(void* arg) {
        auto* self = static_cast<StackChanServo*>(arg);
        int yaw = (rand() % 51) - 25;
        int pitch = 25 + (rand() % 11);
        self->MoveTo(yaw, pitch, 1500);
    }

    SCSCL bus_;
    esp_timer_handle_t idle_timer_ = nullptr;
    FaceTracker* tracker_ = nullptr;
    bool scan_running_ = false;
    volatile bool anim_running_ = false;
    int last_yaw_deg_ = 0;
    int last_pitch_deg_ = 30;
};

class FaceTracker {
    static constexpr int DS_W = 40;
    static constexpr int DS_H = 30;
public:
    void Start(EspVideo* camera, StackChanServo* servo) {
        camera_ = camera;
        servo_ = servo;
        if (!camera_ || !servo_) return;

        memset(prev_frame_, 0, sizeof(prev_frame_));
        paused_ = true;
        xTaskCreatePinnedToCore(TaskFunc, "face_track", 4096, this, 1, &task_, 1);
        ESP_LOGI("FaceTrack", "Started (paused until conversation)");
    }

    void Pause(bool resume_scan = true) {
        if (!paused_) {
            paused_ = true;
            tracking_ = false;
            if (resume_scan) servo_->ResumeScan();
            ESP_LOGI("FaceTrack", "Paused (scan=%d)", resume_scan);
        }
    }

    void Resume() {
        // 当 manual_lock_ 被 LLM 工具锁住时，忽略状态机周期 Resume，
        // 否则 Application::Listening/Speaking 每次刷新都会自动 Resume，
        // 让用户"派蒙转 30 度"立刻被人脸追踪复位。
        if (manual_lock_) return;
        if (paused_) {
            paused_ = false;
            has_prev_ = false;
            servo_->PauseScan();
            ESP_LOGI("FaceTrack", "Resumed");
        }
    }

    // 手动锁：lock=true 后，外部 Resume() 无效（除非 lock=false 解锁再 Resume）。
    // LLM head.move/center 用这个保证位置不被自动 tracker 抢回去。
    void SetManualLock(bool locked) {
        manual_lock_ = locked;
        ESP_LOGI("FaceTrack", "Manual lock %s", locked ? "ENABLED" : "DISABLED");
    }

    bool IsManualLocked() const { return manual_lock_; }

    bool IsPaused() const { return paused_; }
    float GetYaw() const { return yaw_; }
    float GetPitch() const { return pitch_; }

private:
    static void TaskFunc(void* arg) {
        auto* self = static_cast<FaceTracker*>(arg);
        while (true) {
            vTaskDelay(pdMS_TO_TICKS(100));
            if (self->paused_ || !self->camera_->IsOk()) continue;
            self->Track();
        }
    }

    void Track() {
        uint8_t cur_frame[DS_W * DS_H];
        int sum_x = 0, sum_y = 0, count = 0;

        bool ok = camera_->PeekFrame([&](const uint8_t* data, size_t len, uint16_t w, uint16_t h) {
            if (w == 0 || h == 0) return;
            int sx = w / DS_W;
            int sy = h / DS_H;
            for (int dy = 0; dy < DS_H; dy++) {
                for (int dx = 0; dx < DS_W; dx++) {
                    int src_offset = (dy * sy * w + dx * sx) * 2;
                    if ((size_t)src_offset >= len) { cur_frame[dy * DS_W + dx] = 0; continue; }
                    cur_frame[dy * DS_W + dx] = data[src_offset];
                }
            }

            if (!has_prev_) return;

            for (int dy = 3; dy < DS_H - 1; dy++) {
                for (int dx = 1; dx < DS_W - 1; dx++) {
                    int idx = dy * DS_W + dx;
                    uint8_t brightness = cur_frame[idx];
                    if (brightness > 200) continue;
                    int diff = abs((int)cur_frame[idx] - (int)prev_frame_[idx]);
                    if (diff > 20) {
                        sum_x += dx;
                        sum_y += dy;
                        count++;
                    }
                }
            }
        });

        memcpy(prev_frame_, cur_frame, sizeof(prev_frame_));
        if (!has_prev_) { has_prev_ = true; return; }
        if (!ok) return;

        int total_pixels = (DS_W - 2) * (DS_H - 4);
        if (count < 3 || count > total_pixels / 3) {
            no_move_count_++;
            if (no_move_count_ > 6 && tracking_) {
                tracking_ = false;
            }
            return;
        }

        no_move_count_ = 0;
        if (!tracking_) {
            servo_->PauseScan();
            tracking_ = true;
        }

        float cx = (float)sum_x / count;
        float cy = (float)sum_y / count;
        float target_x = (cx - DS_W / 2.0f) / (DS_W / 2.0f);
        float target_y = (cy - DS_H / 2.0f) / (DS_H / 2.0f);

        smooth_x_ = smooth_x_ * 0.3f + target_x * 0.7f;
        smooth_y_ = smooth_y_ * 0.3f + target_y * 0.7f;

        if (fabsf(smooth_x_) < 0.03f) smooth_x_ = 0;
        if (fabsf(smooth_y_) < 0.03f) smooth_y_ = 0;

        yaw_ -= smooth_x_ * 6.0f;
        pitch_ -= smooth_y_ * 4.0f;
        if (yaw_ < -45) yaw_ = -45;
        if (yaw_ > 45) yaw_ = 45;
        if (pitch_ < 5) pitch_ = 5;
        if (pitch_ > 60) pitch_ = 60;

        servo_->MoveTo((int)yaw_, (int)pitch_, 150);
    }

    EspVideo* camera_ = nullptr;
    StackChanServo* servo_ = nullptr;
    TaskHandle_t task_ = nullptr;
    volatile bool paused_ = false;
    volatile bool manual_lock_ = false;  // LLM head 工具锁住时为 true
    bool tracking_ = false;
    bool has_prev_ = false;
    int no_move_count_ = 0;
    float yaw_ = 0.0f;
    float pitch_ = 30.0f;
    float smooth_x_ = 0.0f;
    float smooth_y_ = 0.0f;
    uint8_t prev_frame_[DS_W * DS_H];
};

struct ServoAnimCtx {
    StackChanServo* servo;
    int base_yaw;
    int base_pitch;
};

void StackChanServo::Nod() {
    if (anim_running_) return;
    anim_running_ = true;
    // 用 servo 当前命令位置作 base —— 不用 tracker_->GetYaw()，因为后者只反映
    // 人脸追踪的目标值，不反映 LLM head.move 工具设的硬位置。这样 Nod 在
    // 当前位置上下点头，不会把头甩回 (0, 30)。
    auto* ctx = new ServoAnimCtx{this, GetCurrentYaw(), GetCurrentPitch()};
    if (tracker_) tracker_->Pause(false);
    xTaskCreatePinnedToCore([](void* arg) {
        auto* c = static_cast<ServoAnimCtx*>(arg);
        auto* s = c->servo;
        int y = c->base_yaw, p = c->base_pitch;
        s->MoveTo(y, p - 10, 200);
        vTaskDelay(pdMS_TO_TICKS(250));
        s->MoveTo(y, p + 5, 200);
        vTaskDelay(pdMS_TO_TICKS(250));
        s->MoveTo(y, p - 8, 200);
        vTaskDelay(pdMS_TO_TICKS(250));
        s->MoveTo(y, p, 300);
        vTaskDelay(pdMS_TO_TICKS(300));
        s->anim_running_ = false;
        if (s->tracker_) s->tracker_->Resume();
        delete c;
        vTaskDelete(nullptr);
    }, "nod", 2048, ctx, 2, nullptr, 1);
}

void StackChanServo::Shake() {
    if (anim_running_) return;
    anim_running_ = true;
    auto* ctx = new ServoAnimCtx{this, GetCurrentYaw(), GetCurrentPitch()};
    if (tracker_) tracker_->Pause(false);
    xTaskCreatePinnedToCore([](void* arg) {
        auto* c = static_cast<ServoAnimCtx*>(arg);
        auto* s = c->servo;
        int y = c->base_yaw, p = c->base_pitch;
        s->MoveTo(y - 15, p, 200);
        vTaskDelay(pdMS_TO_TICKS(250));
        s->MoveTo(y + 15, p, 200);
        vTaskDelay(pdMS_TO_TICKS(250));
        s->MoveTo(y - 10, p, 200);
        vTaskDelay(pdMS_TO_TICKS(250));
        s->MoveTo(y, p, 300);
        vTaskDelay(pdMS_TO_TICKS(300));
        s->anim_running_ = false;
        if (s->tracker_) s->tracker_->Resume();
        delete c;
        vTaskDelete(nullptr);
    }, "shake", 2048, ctx, 2, nullptr, 1);
}

void StackChanServo::Tilt() {
    if (anim_running_) return;
    anim_running_ = true;
    auto* ctx = new ServoAnimCtx{this, GetCurrentYaw(), GetCurrentPitch()};
    if (tracker_) tracker_->Pause(false);
    xTaskCreatePinnedToCore([](void* arg) {
        auto* c = static_cast<ServoAnimCtx*>(arg);
        auto* s = c->servo;
        int y = c->base_yaw, p = c->base_pitch;
        s->MoveTo(y + 10, p - 10, 400);
        vTaskDelay(pdMS_TO_TICKS(1500));
        s->MoveTo(y, p, 500);
        vTaskDelay(pdMS_TO_TICKS(500));
        s->anim_running_ = false;
        if (s->tracker_) s->tracker_->Resume();
        delete c;
        vTaskDelete(nullptr);
    }, "tilt", 2048, ctx, 2, nullptr, 1);
}

static bool EnableServoPowerViaPy32(i2c_master_bus_handle_t i2c_bus) {
    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = 0x6F,
        .scl_speed_hz = 100000,
        .scl_wait_us = 0,
        .flags = { .disable_ack_check = 0 },
    };
    i2c_master_dev_handle_t dev = nullptr;
    esp_err_t err = i2c_master_bus_add_device(i2c_bus, &dev_cfg, &dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "PY32: failed to add device: %s", esp_err_to_name(err));
        return false;
    }

    for (int i = 0; i < 10; i++) {
        vTaskDelay(pdMS_TO_TICKS(200));
        uint8_t reg = 0x02;
        uint8_t ver = 0;
        err = i2c_master_transmit_receive(dev, &reg, 1, &ver, 1, 200);
        if (err == ESP_OK && ver != 0 && ver != 0xFF) {
            ESP_LOGI(TAG, "PY32 found! version=%d, enabling VM_EN", ver);
            uint8_t buf[2];
            reg = 0x03; i2c_master_transmit_receive(dev, &reg, 1, &buf[1], 1, 200);
            buf[0] = 0x03; buf[1] |= 0x01; i2c_master_transmit(dev, buf, 2, 200);
            reg = 0x09; i2c_master_transmit_receive(dev, &reg, 1, &buf[1], 1, 200);
            buf[0] = 0x09; buf[1] |= 0x01; i2c_master_transmit(dev, buf, 2, 200);
            reg = 0x05; i2c_master_transmit_receive(dev, &reg, 1, &buf[1], 1, 200);
            buf[0] = 0x05; buf[1] |= 0x01; i2c_master_transmit(dev, buf, 2, 200);
            ESP_LOGI(TAG, "Servo power enabled (VM_EN)");
            return true;
        }
        ESP_LOGD(TAG, "PY32 attempt %d: err=%s ver=0x%02X", i, esp_err_to_name(err), ver);
    }

    ESP_LOGW(TAG, "PY32 not found after 10 attempts");
    i2c_master_bus_rm_device(dev);
    return false;
}

namespace shizhou_avatar {

enum class Expression {
    Neutral, Happy, Angry, Sad, Sleepy,
    Loving, Crying,
    Kissy, Cool, Confident,
    Shocked, Thinking, Surprised, Confused,
    Embarrassed, Silly, Winking, Laughing, Funny, Relaxed, Delicious
};

struct Overlay {
    bool tear = false;
    bool heart_eyes = false;
    bool kiss_heart = false;
    bool cheek_blush = false;
    bool cool_glasses = false;
    bool excl_mark = false;
    bool think_bubble = false;
    bool star_burst = false;
    bool wave_squiggle = false;
    bool drool = false;
    bool laugh_lines = false;
    bool question_mark = false;
    bool zzz = false;
};

class LvglAvatar {
public:
    LvglAvatar() = default;
    ~LvglAvatar() { Destroy(); }

    bool Init(lv_obj_t* parent, int w, int h) {
        if (canvas_) return true;
        w_ = w; h_ = h;
        size_t bytes = (size_t)w * h * 2;
        buf_ = (uint8_t*)heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM);
        if (!buf_) return false;
        canvas_ = lv_canvas_create(parent);
        lv_canvas_set_buffer(canvas_, buf_, w, h, LV_COLOR_FORMAT_RGB565);
        lv_obj_align(canvas_, LV_ALIGN_TOP_LEFT, 0, 0);
        lv_obj_clear_flag(canvas_, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_move_background(canvas_);
        timer_ = lv_timer_create(&LvglAvatar::TimerCb, 50, this);
        next_blink_ms_ = 3000;
        last_saccade_ms_ = 0;
        Draw();
        return true;
    }

    void Destroy() {
        if (timer_) { lv_timer_delete(timer_); timer_ = nullptr; }
        if (canvas_) { lv_obj_delete(canvas_); canvas_ = nullptr; }
        if (buf_)   { heap_caps_free(buf_); buf_ = nullptr; }
    }

    bool IsReady() const { return canvas_ != nullptr; }

    void SetExpression(Expression e) { expression_ = e; }
    void SetOverlay(const Overlay& o) { overlay_ = o; }
    void StartSpeaking(uint32_t duration_ms) {
        speaking_until_ms_ = lv_tick_get() + duration_ms;
    }
    void StopSpeaking() { speaking_until_ms_ = 0; }

private:
    static void TimerCb(lv_timer_t* t) {
        static_cast<LvglAvatar*>(lv_timer_get_user_data(t))->OnTick();
    }

    void UpdateBreathParams() {
        breath_amp_ = 3.0f;
        breath_period_steps_ = 100;
        breath_paused_ = false;
        switch (expression_) {
            case Expression::Relaxed:
                breath_amp_ = 7.0f;
                breath_period_steps_ = 160;
                break;
            case Expression::Shocked:
                breath_paused_ = true;
                break;
            default: break;
        }
    }

    bool BlinkAllowed() const {
        switch (expression_) {
            case Expression::Cool:
            case Expression::Confident:
            case Expression::Shocked:
            case Expression::Winking:
            case Expression::Kissy:
                return false;
            default:
                return true;
        }
    }

    bool SlowBlink() const {
        return expression_ == Expression::Thinking || expression_ == Expression::Relaxed;
    }

    bool SaccadeEnabled() const {
        switch (expression_) {
            case Expression::Cool:
            case Expression::Confident:
            case Expression::Shocked:
            case Expression::Thinking:
            case Expression::Embarrassed:
            case Expression::Winking:
                return false;
            default:
                return true;
        }
    }

    void GetGazeOverride(float* gh, float* gv) const {
        switch (expression_) {
            case Expression::Thinking:
                *gh = 0; *gv = -1.0f; break;
            case Expression::Embarrassed:
                *gh = 0; *gv = 0.7f; break;
            default:
                *gh = gaze_h_; *gv = gaze_v_;
        }
    }

    void OnTick() {
        tick_count_++;
        uint32_t now = lv_tick_get();

        UpdateBreathParams();
        if (breath_paused_) {
            breath_ = 0;
        } else {
            breath_ = sinf((tick_count_ % breath_period_steps_) * 2.0f * 3.14159265f / breath_period_steps_);
        }

        if (BlinkAllowed()) {
            if (now >= next_blink_ms_) {
                uint32_t mult = SlowBlink() ? 2 : 1;
                if (eye_closed_) {
                    eye_open_ratio_ = 1.0f;
                    next_blink_ms_ = now + mult * (2500 + (rand() % 2000));
                    eye_closed_ = false;
                } else {
                    eye_open_ratio_ = 0.0f;
                    next_blink_ms_ = now + 150 + (rand() % 200);
                    eye_closed_ = true;
                }
            }
        } else {
            eye_open_ratio_ = 1.0f;
            eye_closed_ = false;
        }

        if (SaccadeEnabled() && now - last_saccade_ms_ > 1500) {
            gaze_h_ = (rand() % 21 - 10) / 10.0f;
            gaze_v_ = (rand() % 21 - 10) / 10.0f;
            last_saccade_ms_ = now;
        }

        bool speaking = (speaking_until_ms_ != 0 && now < speaking_until_ms_);
        if (!speaking && speaking_until_ms_ != 0) speaking_until_ms_ = 0;
        if (speaking) {
            mouth_open_ = 0.2f + (rand() % 80) / 100.0f;
        } else {
            mouth_open_ = 0.0f;
        }

        Draw();
    }

    void Draw() {
        if (!canvas_) return;
        const lv_color_t bg = lv_color_make(0x00, 0x00, 0x00);
        const lv_color_t fg = lv_color_make(0xFF, 0xFF, 0xFF);

        lv_canvas_fill_bg(canvas_, bg, LV_OPA_COVER);
        lv_layer_t layer;
        lv_canvas_init_layer(canvas_, &layer);

        DrawMouth(&layer, fg, bg);
        DrawEye(&layer, fg, bg, false);
        DrawEye(&layer, fg, bg, true);
        DrawOverlay(&layer, fg, bg);

        lv_canvas_finish_layer(canvas_, &layer);
    }

    void FillRect(lv_layer_t* layer, int x, int y, int w, int h, lv_color_t c) {
        if (w <= 0 || h <= 0) return;
        lv_draw_rect_dsc_t d;
        lv_draw_rect_dsc_init(&d);
        d.bg_color = c;
        d.bg_opa = LV_OPA_COVER;
        d.radius = 0;
        d.border_width = 0;
        lv_area_t a = {x, y, x + w - 1, y + h - 1};
        lv_draw_rect(layer, &d, &a);
    }

    void FillCircle(lv_layer_t* layer, int cx, int cy, int r, lv_color_t c) {
        if (r <= 0) return;
        lv_draw_rect_dsc_t d;
        lv_draw_rect_dsc_init(&d);
        d.bg_color = c;
        d.bg_opa = LV_OPA_COVER;
        d.radius = LV_RADIUS_CIRCLE;
        d.border_width = 0;
        lv_area_t a = {cx - r, cy - r, cx + r - 1, cy + r - 1};
        lv_draw_rect(layer, &d, &a);
    }

    void FillTriangle(lv_layer_t* layer, int x0, int y0, int x1, int y1, int x2, int y2, lv_color_t c) {
        lv_draw_triangle_dsc_t d;
        lv_draw_triangle_dsc_init(&d);
        d.p[0].x = (float)x0; d.p[0].y = (float)y0;
        d.p[1].x = (float)x1; d.p[1].y = (float)y1;
        d.p[2].x = (float)x2; d.p[2].y = (float)y2;
        d.color = c;
        d.opa = LV_OPA_COVER;
        lv_draw_triangle(layer, &d);
    }

    void FillRoundRect(lv_layer_t* layer, int x, int y, int w, int h, int radius, lv_color_t c) {
        if (w <= 0 || h <= 0) return;
        lv_draw_rect_dsc_t d;
        lv_draw_rect_dsc_init(&d);
        d.bg_color = c;
        d.bg_opa = LV_OPA_COVER;
        d.radius = radius;
        d.border_width = 0;
        lv_area_t a = {x, y, x + w - 1, y + h - 1};
        lv_draw_rect(layer, &d, &a);
    }

    void DrawArc(lv_layer_t* layer, int cx, int cy, int r, int start_deg, int end_deg, int width, lv_color_t c, bool rounded = false) {
        lv_draw_arc_dsc_t d;
        lv_draw_arc_dsc_init(&d);
        d.color = c;
        d.opa = LV_OPA_COVER;
        d.width = width;
        d.center.x = cx;
        d.center.y = cy;
        d.radius = r;
        d.start_angle = start_deg;
        d.end_angle = end_deg;
        d.rounded = rounded ? 1 : 0;
        lv_draw_arc(layer, &d);
    }

    void DrawLine(lv_layer_t* layer, int x1, int y1, int x2, int y2, int width, bool round, lv_color_t c) {
        lv_draw_line_dsc_t d;
        lv_draw_line_dsc_init(&d);
        d.color = c;
        d.opa = LV_OPA_COVER;
        d.width = width;
        d.round_start = round ? 1 : 0;
        d.round_end = round ? 1 : 0;
        d.p1.x = (float)x1; d.p1.y = (float)y1;
        d.p2.x = (float)x2; d.p2.y = (float)y2;
        lv_draw_line(layer, &d);
    }

    void DrawMouth(lv_layer_t* layer, lv_color_t fg, lv_color_t bg) {
        const int cx = 163;
        const int cy = 148 + (int)(breath_ * 3.0f);
        const int y_off = (int)(breath_ * 2.0f);

        switch (expression_) {
            case Expression::Cool:
                DrawLine(layer, cx - 12, cy + y_off + 2, cx + 12, cy + y_off, 3, true, fg);
                return;
            case Expression::Confident:
                DrawLine(layer, cx - 14, cy + y_off + 3, cx + 14, cy + y_off, 3, true, fg);
                return;
            case Expression::Silly:
                DrawLine(layer, cx - 13, cy + y_off + 4, cx + 13, cy + y_off, 3, true, fg);
                return;
            case Expression::Embarrassed:
                DrawLine(layer, cx - 12, cy + y_off, cx + 12, cy + y_off, 3, true, fg);
                return;
            case Expression::Kissy: {
                int my = cy + y_off;
                DrawArc(layer, cx, my - 6, 6, 270, 450, 3, fg, false);
                DrawArc(layer, cx, my + 6, 6, 270, 450, 3, fg, false);
                FillCircle(layer, cx, my, 2, fg);
                return;
            }
            case Expression::Winking:
                DrawArc(layer, cx, cy + y_off - 5, 12, 0, 180, 3, fg);
                return;
            case Expression::Laughing: {
                int y_top = cy + y_off - 28;
                FillRoundRect(layer, cx - 40, y_top, 80, 30, 12, fg);
                return;
            }
            case Expression::Funny:
                FillRoundRect(layer, cx - 25, cy + y_off - 10, 50, 20, 8, fg);
                return;
            case Expression::Relaxed:
                DrawLine(layer, cx - 20, cy + y_off, cx + 20, cy + y_off, 4, true, fg);
                return;
            case Expression::Delicious: {
                int h = 4 + (int)((60 - 4) * 0.3f);
                int w = 50 + (int)((90 - 50) * 0.7f);
                FillRoundRect(layer, cx - w / 2, cy + y_off - h / 2, w, h, 7, fg);
                return;
            }
            case Expression::Shocked: {
                FillRoundRect(layer, cx - 25, cy + y_off - 30, 50, 60, 10, fg);
                return;
            }
            case Expression::Surprised: {
                int h = 4 + (int)((60 - 4) * 0.5f);
                int w = 50 + (int)((90 - 50) * 0.5f);
                FillRoundRect(layer, cx - w / 2, cy + y_off - h / 2, w, h, 12, fg);
                return;
            }
            case Expression::Thinking: {
                DrawLine(layer, cx - 15, cy + y_off, cx + 15, cy + y_off, 3, true, fg);
                return;
            }
            case Expression::Confused: {
                DrawLine(layer, cx - 15, cy + y_off, cx + 15, cy + y_off + 2, 3, true, fg);
                return;
            }
            default: {
                int h = 4 + (int)((60 - 4) * mouth_open_);
                int w = 50 + (int)((90 - 50) * (1.0f - mouth_open_));
                int radius = (int)(mouth_open_ * 10);
                FillRoundRect(layer, cx - w / 2, cy + y_off - h / 2, w, h, radius, fg);
                return;
            }
        }
    }

    void DrawEye(lv_layer_t* layer, lv_color_t fg, lv_color_t bg, bool is_left) {
        if (expression_ == Expression::Cool) return;

        const int cx_base = is_left ? 230 : 90;
        const int cy_base_y = is_left ? 96 : 93;
        const int cy = cy_base_y + (int)(breath_ * 3.0f);

        float gh, gv;
        GetGazeOverride(&gh, &gv);
        const int off_x = (int)(gh * 3.0f);
        const int off_y = (int)(gv * 3.0f);

        if (overlay_.heart_eyes) {
            const lv_color_t red = lv_color_make(0xFF, 0x40, 0x70);
            int hcx = cx_base + off_x;
            int hcy = cy + off_y;
            FillCircle(layer, hcx - 6, hcy - 3, 7, red);
            FillCircle(layer, hcx + 6, hcy - 3, 7, red);
            FillTriangle(layer, hcx - 12, hcy + 1, hcx + 12, hcy + 1, hcx, hcy + 13, red);
            return;
        }

        if (expression_ == Expression::Shocked) {
            FillCircle(layer, cx_base, cy, 13, fg);
            FillCircle(layer, cx_base, cy, 3, bg);
            return;
        }

        if (expression_ == Expression::Surprised) {
            FillCircle(layer, cx_base, cy, 10, fg);
            return;
        }

        if (expression_ == Expression::Confused) {
            int r = is_left ? 8 : 6;
            FillCircle(layer, cx_base + off_x, cy + off_y, r, fg);
            return;
        }

        if (expression_ == Expression::Winking) {
            if (is_left) {
                DrawLine(layer, cx_base + 8, cy - 4, cx_base - 8, cy, 5, true, fg);
                DrawLine(layer, cx_base - 8, cy, cx_base + 8, cy + 4, 5, true, fg);
            } else {
                FillCircle(layer, cx_base, cy, 8, fg);
            }
            return;
        }

        if (expression_ == Expression::Silly) {
            int r = 8;
            FillCircle(layer, cx_base + off_x, cy + off_y, r, fg);
            int x0 = cx_base + off_x - r;
            int y0 = cy + off_y;
            int w = r * 2 + 4;
            int h = r + 2;
            if (!is_left) h += 2;
            FillCircle(layer, cx_base + off_x, cy + off_y, (int)(r / 1.5f), bg);
            FillRect(layer, x0, y0, w, h, bg);
            return;
        }

        if (expression_ == Expression::Laughing) {
            int r = 8;
            FillCircle(layer, cx_base + off_x, cy + off_y, r, fg);
            int x0 = cx_base + off_x - r - 2;
            int y0 = cy + off_y;
            int w = r * 2 + 8;
            int h = r + 4;
            FillCircle(layer, cx_base + off_x, cy + off_y, (int)(r / 1.5f), bg);
            FillRect(layer, x0, y0, w, h, bg);
            return;
        }

        if (expression_ == Expression::Sleepy) {
            if (is_left) {
                DrawLine(layer, cx_base - 8 + off_x, cy - 2 + off_y,
                                cx_base + 8 + off_x, cy + 2 + off_y, 4, true, fg);
            } else {
                DrawLine(layer, cx_base - 8 + off_x, cy + 2 + off_y,
                                cx_base + 8 + off_x, cy - 2 + off_y, 4, true, fg);
            }
            return;
        }

        if (expression_ == Expression::Relaxed) {
            int r = 8;
            FillCircle(layer, cx_base + off_x, cy + off_y, r, fg);
            int x0 = cx_base + off_x - r - 1;
            int y0 = cy + off_y - 1;
            int w = r * 2 + 6;
            int h = r + 3;
            FillCircle(layer, cx_base + off_x, cy + off_y, (int)(r / 1.5f), bg);
            FillRect(layer, x0, y0, w, h, bg);
            return;
        }

        const int r = 8;

        if (eye_open_ratio_ > 0) {
            FillCircle(layer, cx_base + off_x, cy + off_y, r, fg);

            if (expression_ == Expression::Angry || expression_ == Expression::Sad || expression_ == Expression::Crying) {
                int x0 = cx_base + off_x - r;
                int y0 = cy + off_y - r;
                int x1 = x0 + r * 2;
                int y1 = y0;
                bool sad = (expression_ == Expression::Sad || expression_ == Expression::Crying);
                int x2 = ((!is_left) != (!sad)) ? x0 : x1;
                int y2 = y0 + r;
                FillTriangle(layer, x0, y0, x1, y1, x2, y2, bg);
            }

            if (expression_ == Expression::Happy
                || expression_ == Expression::Kissy || expression_ == Expression::Funny
                || expression_ == Expression::Delicious) {
                FillCircle(layer, cx_base + off_x, cy + off_y, r + 2, bg);
                DrawArc(layer, cx_base + off_x, cy + off_y + r,
                        r, 180, 360, 3, fg, true);
            }
        } else {
            FillRect(layer, cx_base - r + off_x, cy - 2 + off_y, r * 2, 4, fg);
        }
    }

    void DrawOverlay(lv_layer_t* layer, lv_color_t fg, lv_color_t bg) {
        if (overlay_.tear) {
            const lv_color_t blue = lv_color_make(0x40, 0xA0, 0xFF);
            int tx = 90;
            int ty = 115 + (int)(breath_ * 3.0f);
            FillCircle(layer, tx, ty, 7, blue);
            FillTriangle(layer, tx - 6, ty - 2, tx + 6, ty - 2, tx, ty - 15, blue);
        }

        if (overlay_.cheek_blush) {
            const lv_color_t pink = lv_color_make(0xFF, 0x64, 0x82);
            for (int i = 0; i < 3; i++) {
                int x_start = 47 + i * 8;
                int x_end = x_start + 6;
                DrawLine(layer, x_start, 138, x_end, 130, 3, true, pink);
            }
            for (int i = 0; i < 3; i++) {
                int x_start = 251 + i * 8;
                int x_end = x_start + 6;
                DrawLine(layer, x_start, 138, x_end, 130, 3, true, pink);
            }
        }

        if (overlay_.cool_glasses) {
            FillRoundRect(layer, 85, 84, 50, 24, 5, fg);
            FillRoundRect(layer, 185, 84, 50, 24, 5, fg);
            DrawLine(layer, 85, 84, 235, 84, 2, false, fg);
        }

        if (overlay_.excl_mark) {
            DrawLine(layer, 291, 50, 291, 68, 4, true, fg);
            FillCircle(layer, 291, 76, 2, fg);
        }

        if (overlay_.think_bubble) {
            FillRoundRect(layer, 245, 47, 50, 25, 12, fg);
            FillCircle(layer, 258, 60, 3, bg);
            FillCircle(layer, 270, 60, 3, bg);
            FillCircle(layer, 282, 60, 3, bg);
            FillCircle(layer, 273, 85, 6, fg);
            FillCircle(layer, 258, 110, 4, fg);
        }

        if (overlay_.star_burst) {
            const int cx_s = 290, cy_s = 60;
            FillRect(layer, cx_s - 3, cy_s - 3, 6, 6, fg);
            FillTriangle(layer, cx_s, cy_s - 18, cx_s - 3, cy_s - 3, cx_s + 3, cy_s - 3, fg);
            FillTriangle(layer, cx_s, cy_s + 18, cx_s - 3, cy_s + 3, cx_s + 3, cy_s + 3, fg);
            FillTriangle(layer, cx_s - 18, cy_s, cx_s - 3, cy_s - 3, cx_s - 3, cy_s + 3, fg);
            FillTriangle(layer, cx_s + 18, cy_s, cx_s + 3, cy_s - 3, cx_s + 3, cy_s + 3, fg);
        }

        if (overlay_.wave_squiggle) {
            DrawLine(layer, 148, 28, 154, 24, 2, true, fg);
            DrawLine(layer, 154, 24, 160, 28, 2, true, fg);
            DrawLine(layer, 160, 28, 166, 24, 2, true, fg);
            DrawLine(layer, 166, 24, 172, 28, 2, true, fg);
        }

        if (overlay_.drool) {
            const lv_color_t blue = lv_color_make(0x40, 0xA0, 0xFF);
            int dx = 143;
            int dy = 168 + (int)(breath_ * 3.0f);
            FillCircle(layer, dx, dy, 4, blue);
            FillTriangle(layer, dx - 3, dy - 2, dx + 3, dy - 2, dx, dy - 8, blue);
        }

        if (overlay_.laugh_lines) {
            DrawLine(layer, 210, 150, 220, 142, 3, true, fg);
            DrawLine(layer, 218, 156, 228, 148, 3, true, fg);
        }

        if (overlay_.question_mark) {
            DrawArc(layer, 290, 50, 7, 180, 90, 4, fg, true);
            FillCircle(layer, 290, 67, 3, fg);
        }

        if (overlay_.zzz) {
            auto draw_z = [&](int cx_z, int cy_z, int size, int w) {
                int h = size / 2;
                DrawLine(layer, cx_z - h, cy_z - h, cx_z + h, cy_z - h, w, false, fg);
                DrawLine(layer, cx_z + h, cy_z - h, cx_z - h, cy_z + h, w, false, fg);
                DrawLine(layer, cx_z - h, cy_z + h, cx_z + h, cy_z + h, w, false, fg);
            };
            draw_z(258, 80, 8, 2);
            draw_z(270, 70, 10, 3);
            draw_z(286, 55, 14, 3);
        }

        if (overlay_.kiss_heart) {
            const lv_color_t red = lv_color_make(0xFF, 0x40, 0x70);
            int hx = 195;
            int hy = 130;
            FillCircle(layer, hx - 3, hy - 1, 4, red);
            FillCircle(layer, hx + 3, hy - 1, 4, red);
            FillTriangle(layer, hx - 6, hy + 1, hx + 6, hy + 1, hx, hy + 8, red);
        }
    }

    lv_obj_t* canvas_ = nullptr;
    uint8_t* buf_ = nullptr;
    lv_timer_t* timer_ = nullptr;
    int w_ = 320, h_ = 240;

    Expression expression_ = Expression::Neutral;
    Overlay overlay_;

    uint32_t tick_count_ = 0;
    uint32_t next_blink_ms_ = 0;
    uint32_t last_saccade_ms_ = 0;
    uint32_t speaking_until_ms_ = 0;
    bool eye_closed_ = false;
    float eye_open_ratio_ = 1.0f;
    float mouth_open_ = 0.0f;
    float breath_ = 0.0f;
    float gaze_h_ = 0.0f;
    float gaze_v_ = 0.0f;

    float breath_amp_ = 3.0f;
    uint32_t breath_period_steps_ = 100;
    bool breath_paused_ = false;
};

static Expression MapEmotion(const char* e) {
    if (!e) return Expression::Neutral;
    if (!strcmp(e, "neutral"))     return Expression::Neutral;
    if (!strcmp(e, "happy"))       return Expression::Happy;
    if (!strcmp(e, "laughing"))    return Expression::Laughing;
    if (!strcmp(e, "funny"))       return Expression::Funny;
    if (!strcmp(e, "sad"))         return Expression::Sad;
    if (!strcmp(e, "crying"))      return Expression::Crying;
    if (!strcmp(e, "angry"))       return Expression::Angry;
    if (!strcmp(e, "loving"))      return Expression::Loving;
    if (!strcmp(e, "embarrassed")) return Expression::Embarrassed;
    if (!strcmp(e, "surprised"))   return Expression::Surprised;
    if (!strcmp(e, "shocked"))     return Expression::Shocked;
    if (!strcmp(e, "thinking"))    return Expression::Thinking;
    if (!strcmp(e, "winking"))     return Expression::Winking;
    if (!strcmp(e, "cool"))        return Expression::Cool;
    if (!strcmp(e, "relaxed"))     return Expression::Relaxed;
    if (!strcmp(e, "delicious"))   return Expression::Delicious;
    if (!strcmp(e, "kissy"))       return Expression::Kissy;
    if (!strcmp(e, "confident"))   return Expression::Confident;
    if (!strcmp(e, "sleepy"))      return Expression::Sleepy;
    if (!strcmp(e, "silly"))       return Expression::Silly;
    if (!strcmp(e, "confused"))    return Expression::Confused;
    return Expression::Neutral;
}

static Overlay OverlayFor(const char* e) {
    Overlay o;
    if (!e) return o;
    if (!strcmp(e, "crying"))      o.tear = true;
    if (!strcmp(e, "loving"))      o.heart_eyes = true;
    if (!strcmp(e, "kissy"))       o.kiss_heart = true;
    if (!strcmp(e, "embarrassed")) o.cheek_blush = true;
    if (!strcmp(e, "cool"))        o.cool_glasses = true;
    if (!strcmp(e, "shocked"))     o.excl_mark = true;
    if (!strcmp(e, "thinking"))    o.think_bubble = true;
    if (!strcmp(e, "surprised"))   o.star_burst = true;
    // silly: no overlay
    if (!strcmp(e, "delicious"))   o.drool = true;
    if (!strcmp(e, "confused"))    o.question_mark = true;
    if (!strcmp(e, "sleepy"))      o.zzz = true;
    return o;
}

}  // namespace shizhou_avatar

class M5StackAvatarDisplay : public SpiLcdDisplay {
public:
    M5StackAvatarDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                          int width, int height, int offset_x, int offset_y,
                          bool mirror_x, bool mirror_y, bool swap_xy)
        : SpiLcdDisplay(panel_io, panel, width, height, offset_x, offset_y, mirror_x, mirror_y, swap_xy),
          canvas_w_(width), canvas_h_(height) {
        esp_timer_create_args_t args = {};
        args.callback = &M5StackAvatarDisplay::InitTimerCallback;
        args.arg = this;
        args.dispatch_method = ESP_TIMER_TASK;
        args.name = "avatar_init";
        args.skip_unhandled_events = true;
        esp_timer_create(&args, &avatar_init_timer_);
        esp_timer_start_periodic(avatar_init_timer_, 500000);

        esp_timer_create_args_t idle_args = {};
        idle_args.callback = &M5StackAvatarDisplay::IdleTimerCallback;
        idle_args.arg = this;
        idle_args.dispatch_method = ESP_TIMER_TASK;
        idle_args.name = "avatar_idle";
        idle_args.skip_unhandled_events = true;
        esp_timer_create(&idle_args, &idle_timer_);
    }

    void SetFaceTracker(FaceTracker* ft) { face_tracker_ = ft; }
    void SetServo(StackChanServo* s) { servo_ = s; }
    void SetLedUpdater(std::function<void(const char*)> fn) { led_updater_ = std::move(fn); }

    void OnPetted() {
        if (!avatar_.IsReady()) return;
        DisplayLockGuard lock(this);
        avatar_.SetExpression(shizhou_avatar::Expression::Loving);
        shizhou_avatar::Overlay o;
        o.heart_eyes = true;
        o.cheek_blush = true;
        avatar_.SetOverlay(o);
        SetActiveLocked(true);
        BumpIdleTimerLocked();
        if (servo_) servo_->Tilt();
    }

    void SetEmotion(const char* emotion) override {
        SpiLcdDisplay::SetEmotion(emotion);
        DisplayLockGuard lock(this);
        HideEmojiBoxLocked();
        if (!avatar_.IsReady()) return;
        avatar_.SetExpression(shizhou_avatar::MapEmotion(emotion));
        avatar_.SetOverlay(shizhou_avatar::OverlayFor(emotion));
        if (emotion && strcmp(emotion, "sleepy") == 0) {
            SetActiveLocked(false);
            if (face_tracker_) face_tracker_->Pause(false);
            if (servo_) servo_->PauseScan();
        }
        // 非 sleepy 不再无条件 Resume face_tracker——它现在跟设备状态走，由 SetStatus 控制
        // 注意：head_locked 时 Nod/Shake 仍然会播放，但因为 Nod/Shake 用
        // GetCurrentYaw/Pitch 作 base，会在当前位置就地点头/摇头，不会把头甩回 (0, 30)。
        if (servo_ && emotion && !servo_->IsAnimating()) {
            if (!strcmp(emotion, "happy") || !strcmp(emotion, "loving") ||
                !strcmp(emotion, "laughing") || !strcmp(emotion, "confident") ||
                !strcmp(emotion, "winking") || !strcmp(emotion, "delicious")) {
                servo_->Nod();
            } else if (!strcmp(emotion, "sad") || !strcmp(emotion, "confused") ||
                       !strcmp(emotion, "angry") || !strcmp(emotion, "shocked")) {
                servo_->Shake();
            }
        }
        // 情绪灯联动
        if (led_updater_) led_updater_(emotion);
    }

    void SetPreviewImage(std::unique_ptr<LvglImage> image) override {
        SpiLcdDisplay::SetPreviewImage(std::move(image));
        DisplayLockGuard lock(this);
        HideEmojiBoxLocked();
    }

    void SetChatMessage(const char* role, const char* content) override {
        SpiLcdDisplay::SetChatMessage(role, content);
        DisplayLockGuard lock(this);
        if (!avatar_.IsReady()) return;
        bool meaningful = role && content && content[0] != '\0'
            && (strcmp(role, "user") == 0 || strcmp(role, "assistant") == 0);
        if (meaningful) {
            SetActiveLocked(true);
            BumpIdleTimerLocked();
        }
        if (role && content && content[0] != '\0' && strcmp(role, "assistant") == 0) {
            size_t n = strlen(content);
            uint32_t ms = (uint32_t)(n * 120);
            if (ms < 800) ms = 800;
            if (ms > 15000) ms = 15000;
            avatar_.StartSpeaking(ms);
        } else if (role && (strcmp(role, "user") == 0 || strcmp(role, "system") == 0)) {
            avatar_.StopSpeaking();
        }
    }

    void SetStatus(const char* status) override {
        SpiLcdDisplay::SetStatus(status);
        if (!status || !avatar_.IsReady()) return;
        DisplayLockGuard lock(this);
        auto state = Application::GetInstance().GetDeviceState();
        if (state == kDeviceStateListening || state == kDeviceStateSpeaking) {
            if (face_tracker_) face_tracker_->Resume();
        } else if (state == kDeviceStateIdle) {
            if (face_tracker_) face_tracker_->Pause();
        }
        bool is_active = (strstr(status, "聆听")
                       || strstr(status, "说话")
                       || strstr(status, "思考")
                       || strstr(status, "连接")
                       || strstr(status, "Listening")
                       || strstr(status, "Speaking")
                       || strstr(status, "Thinking")
                       || strstr(status, "Connecting"));
        if (is_active) {
            SetActiveLocked(true);
            BumpIdleTimerLocked();
        }
    }

private:
    shizhou_avatar::LvglAvatar avatar_;
    FaceTracker* face_tracker_ = nullptr;
    StackChanServo* servo_ = nullptr;
    std::function<void(const char*)> led_updater_;
    esp_timer_handle_t avatar_init_timer_ = nullptr;
    esp_timer_handle_t idle_timer_ = nullptr;
    bool active_mode_ = false;
    int canvas_w_ = 320;
    int canvas_h_ = 240;
    static constexpr uint64_t IDLE_TIMEOUT_US = 8 * 1000 * 1000;

    void HideEmojiBoxLocked() {
        if (avatar_.IsReady() && emoji_box_) {
            lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
        }
    }

    void SetActiveLocked(bool active) {
        if (active == active_mode_) return;
        active_mode_ = active;
        if (active) {
            if (top_bar_)    lv_obj_remove_flag(top_bar_, LV_OBJ_FLAG_HIDDEN);
            if (status_bar_) lv_obj_remove_flag(status_bar_, LV_OBJ_FLAG_HIDDEN);
            if (face_tracker_) face_tracker_->Resume();
        } else {
            if (top_bar_)    lv_obj_add_flag(top_bar_, LV_OBJ_FLAG_HIDDEN);
            if (status_bar_) lv_obj_add_flag(status_bar_, LV_OBJ_FLAG_HIDDEN);
        }
    }

    void BumpIdleTimerLocked() {
        if (!idle_timer_) return;
        esp_timer_stop(idle_timer_);
        esp_timer_start_once(idle_timer_, IDLE_TIMEOUT_US);
    }

    static void IdleTimerCallback(void* arg) {
        auto self = static_cast<M5StackAvatarDisplay*>(arg);
        DisplayLockGuard lock(self);
        self->SetActiveLocked(false);
        // face_tracker 由 SetStatus 跟设备状态联动控制，这里只管 UI 顶栏隐藏
    }

    static void InitTimerCallback(void* arg) {
        auto self = static_cast<M5StackAvatarDisplay*>(arg);
        self->TryInitAvatar();
    }

    void TryInitAvatar() {
        DisplayLockGuard lock(this);
        if (avatar_.IsReady()) return;
        lv_obj_t* screen = lv_screen_active();
        if (screen == nullptr) return;
        if (container_ == nullptr) return;

        bool ok = avatar_.Init(screen, canvas_w_, canvas_h_);
        if (!ok) return;

        lv_obj_set_style_bg_opa(container_, LV_OPA_TRANSP, 0);
        if (content_) lv_obj_set_style_bg_opa(content_, LV_OPA_TRANSP, 0);
        if (emoji_box_) {
            lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
        }
        if (top_bar_)    lv_obj_add_flag(top_bar_, LV_OBJ_FLAG_HIDDEN);
        if (status_bar_) lv_obj_add_flag(status_bar_, LV_OBJ_FLAG_HIDDEN);

        if (avatar_init_timer_) {
            esp_timer_stop(avatar_init_timer_);
            esp_timer_delete(avatar_init_timer_);
            avatar_init_timer_ = nullptr;
        }
    }
};

class Pmic : public Axp2101 {
public:
    // Power Init
    Pmic(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : Axp2101(i2c_bus, addr) {
        uint8_t data = ReadReg(0x90);
        data |= 0b10110100;
        WriteReg(0x90, data);
        WriteReg(0x99, (0b11110 - 5));
        WriteReg(0x97, (0b11110 - 2));
        WriteReg(0x69, 0b00110101);
        WriteReg(0x30, 0b111111);
        WriteReg(0x90, 0xBF);
        WriteReg(0x94, 33 - 5);
        WriteReg(0x95, 33 - 5);
    }

    void SetBrightness(uint8_t brightness) {
        brightness = ((brightness + 641) >> 5);
        WriteReg(0x99, brightness);
    }
};

class CustomBacklight : public Backlight {
public:
    CustomBacklight(Pmic *pmic) : pmic_(pmic) {}

    void SetBrightnessImpl(uint8_t brightness) override {
        pmic_->SetBrightness(target_brightness_);
        brightness_ = target_brightness_;
    }

private:
    Pmic *pmic_;
};

class Aw9523 : public I2cDevice {
public:
    // Exanpd IO Init
    Aw9523(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        WriteReg(0x02, 0b00000111);  // P0
        WriteReg(0x03, 0b10001111);  // P1
        WriteReg(0x04, 0b00011000);  // CONFIG_P0
        WriteReg(0x05, 0b00001100);  // CONFIG_P1
        WriteReg(0x11, 0b00010000);  // GCR P0 port is Push-Pull mode.
        WriteReg(0x12, 0b11111111);  // LEDMODE_P0
        WriteReg(0x13, 0b11111111);  // LEDMODE_P1
    }

    void ResetAw88298() {
        ESP_LOGI(TAG, "Reset AW88298");
        WriteReg(0x02, 0b00000011);
        vTaskDelay(pdMS_TO_TICKS(10));
        WriteReg(0x02, 0b00000111);
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    void ResetIli9342() {
        ESP_LOGI(TAG, "Reset IlI9342");
        WriteReg(0x03, 0b10000001);
        vTaskDelay(pdMS_TO_TICKS(20));
        WriteReg(0x03, 0b10000011);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
};

class Ft6336 : public I2cDevice {
public:
    struct TouchPoint_t {
        int num = 0;
        int x = -1;
        int y = -1;
    };
    
    Ft6336(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        uint8_t chip_id = ReadReg(0xA3);
        ESP_LOGI(TAG, "Get chip ID: 0x%02X", chip_id);
        read_buffer_ = new uint8_t[6];
    }

    ~Ft6336() {
        delete[] read_buffer_;
    }

    void UpdateTouchPoint() {
        ReadRegs(0x02, read_buffer_, 6);
        tp_.num = read_buffer_[0] & 0x0F;
        tp_.x = ((read_buffer_[1] & 0x0F) << 8) | read_buffer_[2];
        tp_.y = ((read_buffer_[3] & 0x0F) << 8) | read_buffer_[4];
    }

    inline const TouchPoint_t& GetTouchPoint() {
        return tp_;
    }

private:
    uint8_t* read_buffer_ = nullptr;
    TouchPoint_t tp_;
};

class M5StackCoreS3Board;

// 状态直驱 LED: 每次设备状态变化(connecting/listening/speaking/idle)由
// Application 调 led->OnStateChanged(), 直接写 PY32 灯环, 不依赖 avatar/emotion 路径。
class StateLed : public Led {
public:
    explicit StateLed(M5StackCoreS3Board* board) : board_(board) {}
    void OnStateChanged() override;
private:
    M5StackCoreS3Board* board_;
};

class M5StackCoreS3Board : public WifiBoard {
private:
    i2c_master_bus_handle_t i2c_bus_;
    // Phase 7.1: I2C 共享总线互斥锁(触屏/音频/AXP/PY32 同总线), FreeRTOS Mutex 带优先级继承
    SemaphoreHandle_t i2c_bus_mutex_ = nullptr;
    int touch_i2c_fail_count_ = 0;      // 触屏连续 I2C 超时计数(用于自恢复冷却)
    int64_t touch_pause_until_ms_ = 0;  // 触屏 I2C 故障冷却(500ms 内跳过轮询)
    Pmic* pmic_;
    Aw9523* aw9523_;
    Ft6336* ft6336_;
    LcdDisplay* display_;
    EspVideo* camera_;
    StackChanServo servo_;
    FaceTracker face_tracker_;
    esp_timer_handle_t touchpad_timer_;
    esp_timer_handle_t batt_timer_ = nullptr;
    PowerSaveTimer* power_save_timer_;
    bool py32_found_ = false;
    // ---- PY32 持久 I2C 设备句柄（控 LED + 其他扩展）----
    i2c_master_dev_handle_t py32_dev_ = nullptr;
    bool led_manual_ = false;
    uint16_t manual_color_ = 0;   // 手动色(待机时恢复用), set_color/turn_off 写入
    const char* last_state_eff_ = nullptr;   // 上次状态色(变化才写/才打日志)
    StateLed state_led_{this};
    // ---- BMI270 (IMU) ----
    i2c_master_dev_handle_t bmi_i2c_dev_ = nullptr;
    struct bmi2_dev bmi_dev_storage_ = {};
    bmi270_handle_t bmi_handle_ = nullptr;
    TaskHandle_t motion_task_ = nullptr;
    int64_t last_motion_trigger_us_ = 0;
    // Phase 7.1: 双击后短暂屏蔽体感/摸头手势, 防触屏震动被误判为摇晃/抱起
    volatile int64_t gesture_suppress_until_us_ = 0;
    // Phase 7.1: 双击成功后 300ms 触屏防抖锁, 防电容屏抖动脉冲二次重入 SetListeningMode
    volatile int64_t touch_lock_until_ms_ = 0;

    bool LockI2cBus() {
        if (i2c_bus_mutex_ == nullptr) return true;  // 未初始化时不做互斥
        // Phase 7.1: 50ms 极短超时, 严禁 portMAX_DELAY(防优先级翻转死锁); 超时跳过本次访问
        return xSemaphoreTake(i2c_bus_mutex_, pdMS_TO_TICKS(50)) == pdTRUE;
    }
    void UnlockI2cBus() {
        if (i2c_bus_mutex_ != nullptr) xSemaphoreGive(i2c_bus_mutex_);
    }
    SemaphoreHandle_t GetI2cBusMutex() { return i2c_bus_mutex_; }
    // ---- SI12T 3 区触摸 ----
    i2c_master_bus_handle_t si12t_bus_ = nullptr;
    i2c_master_dev_handle_t si12t_dev_ = nullptr;
    TaskHandle_t si12t_task_ = nullptr;
    uint8_t si12t_last_state_ = 0;
    bool servo_ok_ = false;
    bool low_batt_warned_ = false;

    void InitializeBmi270() {
        // BMI270 实际在 0x69（不是 SDK 默认的 0x68），自己用 IDF i2c API + 底层 bmi270_init 绕过硬编码
        i2c_device_config_t dev_cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = 0x69,
            .scl_speed_hz = 400000,
        };
        if (i2c_master_bus_add_device(i2c_bus_, &dev_cfg, &bmi_i2c_dev_) != ESP_OK) {
            ESP_LOGW(TAG, "BMI270: add i2c device failed");
            return;
        }

        // 自己构造 bmi2_dev：用我们的 i2c_master_dev_handle 作为 intf_ptr，read/write 走 0x69
        bmi_dev_storage_.intf = BMI2_I2C_INTF;
        bmi_dev_storage_.intf_ptr = bmi_i2c_dev_;
        bmi_dev_storage_.read = Bmi270I2cRead;
        bmi_dev_storage_.write = Bmi270I2cWrite;
        bmi_dev_storage_.delay_us = Bmi270DelayUs;
        bmi_dev_storage_.read_write_len = 256;
        bmi_dev_storage_.config_file_ptr = bmi270_config_file;
        bmi_dev_storage_.dummy_byte = 0;

        int8_t rslt = bmi270_init(&bmi_dev_storage_);
        if (rslt != BMI2_OK) {
            ESP_LOGW(TAG, "bmi270_init failed: %d", rslt);
            return;
        }
        ESP_LOGI(TAG, "BMI270 initialized (custom driver @ 0x69, chip_id=0x%02X)", bmi_dev_storage_.chip_id);

        bmi_handle_ = &bmi_dev_storage_;

        const uint8_t sens_list[] = {BMI2_ACCEL};
        rslt = bmi270_sensor_enable(sens_list, 1, bmi_handle_);
        if (rslt != BMI2_OK) {
            ESP_LOGW(TAG, "bmi270_sensor_enable failed: %d", rslt);
            bmi_handle_ = nullptr;
            return;
        }
        ESP_LOGI(TAG, "BMI270 accel enabled");

        xTaskCreatePinnedToCore(MotionTaskFunc, "motion", 4096, this, 1, &motion_task_, 1);
    }

    static void MotionTaskFunc(void* arg) {
        static_cast<M5StackCoreS3Board*>(arg)->MotionLoop();
        vTaskDelete(nullptr);
    }

    void MotionLoop() {
        // 算法：每 100ms 读一次 accel，magnitude 偏离 1g 算"动"
        //   - 1 秒内出现 ≥3 次"动"尖峰 → 摇晃
        //   - 连续 ≥5 个样本（500ms）持续偏离 → 抱起
        //   - 触发后 disarm，必须连续静止 1 秒（10 个样本）才 re-arm
        const float MOTION_THRESHOLD = 0.3f;       // delta 或 mag 偏离 1g 超过此值算"动"
        const int SHAKE_PEAKS_TO_TRIGGER = 2;      // 1 秒内 2 个尖峰算摇晃
        const int64_t SHAKE_WINDOW_US = 1000 * 1000;
        const int LIFT_SAMPLES_TO_TRIGGER = 5;
        const int STILL_SAMPLES_TO_REARM = 50;     // 5 秒静止才允许下次触发
        const int64_t GLOBAL_COOLDOWN_US = 5 * 60 * 1000 * 1000LL;  // 触发后 5 分钟全局冷却

        int lift_count = 0;
        int still_count = 0;
        bool armed = true;
        int64_t shake_peak_times[8] = {0};
        int shake_idx = 0;
        int log_counter = 0;
        float last_ax = 0, last_ay = 0, last_az = 0;
        bool last_valid = false;

        while (true) {
            vTaskDelay(pdMS_TO_TICKS(100));
            if (!bmi_handle_) continue;

            struct bmi2_sens_data accel;
            int8_t rd = bmi2_get_sensor_data(&accel, bmi_handle_);
            if (rd != BMI2_OK) {
                if (++log_counter >= 10) {
                    log_counter = 0;
                    ESP_LOGW(TAG, "motion: bmi2_get_sensor_data err=%d", rd);
                }
                continue;
            }

            // BMI270 SDK 默认 ±8g 量程，int16 raw，1g ≈ 4096
            float ax = (float)accel.acc.x / 4096.0f;
            float ay = (float)accel.acc.y / 4096.0f;
            float az = (float)accel.acc.z / 4096.0f;
            float mag = sqrtf(ax * ax + ay * ay + az * az);

            // 用"轴变化率"检测动作（旋转/摇晃只改变各轴分量但不改变 mag）
            float delta = 0.0f;
            if (last_valid) {
                float dx = ax - last_ax;
                float dy = ay - last_ay;
                float dz = az - last_az;
                delta = sqrtf(dx * dx + dy * dy + dz * dz);
            }
            last_ax = ax; last_ay = ay; last_az = az; last_valid = true;

            // 动 = 轴变化大 OR magnitude 偏离 1g 大
            bool moving = (delta > MOTION_THRESHOLD) || (fabsf(mag - 1.0f) > MOTION_THRESHOLD);
            int64_t now = esp_timer_get_time();
            (void)log_counter;  // 诊断 log 已禁用

            if (!moving) {
                still_count++;
                if (still_count >= STILL_SAMPLES_TO_REARM) armed = true;
                lift_count = 0;
                for (int i = 0; i < 8; i++) shake_peak_times[i] = 0;
                continue;
            }

            still_count = 0;

            // 设备说话时忽略体感（舵机晃动会触发 IMU 误检测）
            if (Application::GetInstance().GetDeviceState() == kDeviceStateSpeaking) continue;

            if (!armed) continue;  // 已触发过，等静止 re-arm
            // 全局冷却：上次触发后 5 分钟内任何情况都不再触发
            if (last_motion_trigger_us_ != 0 && (now - last_motion_trigger_us_) < GLOBAL_COOLDOWN_US) continue;
            // Phase 7.1: 双击后 1.5s 内屏蔽体感, 防触屏震动被误判为摇晃/抱起
            if (now < gesture_suppress_until_us_) continue;

            // 摇晃检测：1 秒内累计 ≥3 个尖峰
            shake_peak_times[shake_idx % 8] = now;
            shake_idx++;
            int peak_count = 0;
            for (int i = 0; i < 8; i++) {
                if (shake_peak_times[i] > 0 &&
                    (now - shake_peak_times[i]) < SHAKE_WINDOW_US) {
                    peak_count++;
                }
            }
            if (peak_count >= SHAKE_PEAKS_TO_TRIGGER) {
                armed = false;
                lift_count = 0;
                last_motion_trigger_us_ = now;
                for (int i = 0; i < 8; i++) shake_peak_times[i] = 0;
                const auto& m = PickRandom(ShakePool());
                if (m.display) {
                    if (auto* disp = GetDisplay()) disp->SetChatMessage("user", m.display);
                    HandleGestureLocally(m.tag);  // 仅本地日志, 不进 LLM
                }
                continue;
            }

            // 抱起检测：连续 ≥5 个样本持续偏离
            lift_count++;
            if (lift_count >= LIFT_SAMPLES_TO_TRIGGER) {
                armed = false;
                lift_count = 0;
                last_motion_trigger_us_ = now;
                const auto& m = PickRandom(LiftPool());
                if (m.display) {
                    if (auto* disp = GetDisplay()) disp->SetChatMessage("user", m.display);
                    HandleGestureLocally(m.tag);  // 仅本地日志, 不进 LLM
                }
            }
        }
    }

    // (Morning greeting + weather timer task removed; see git history.)

    // ---- PY32 IO Expander 控 WS2812 RGB LED ×12 ----
    // 协议（来自 M5Stack/StackChan-BSP）:
    //   REG_LED_CFG  (0x24): 低6位=LED数量, bit6=1 触发刷新
    //   REG_LED_RAM  (0x30+): 颜色数据起点，每颗 LED 2 字节 RGB565 little-endian

    void InitializePy32LedDevice() {
        i2c_device_config_t dev_cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = 0x6F,
            .scl_speed_hz = 100000,
        };
        if (i2c_master_bus_add_device(i2c_bus_, &dev_cfg, &py32_dev_) != ESP_OK) {
            ESP_LOGW(TAG, "PY32 LED: add device failed");
            py32_dev_ = nullptr;
            return;
        }
        // 设 LED 数量 = 12
        uint8_t cmd[2] = {0x24, 12};
        i2c_master_transmit(py32_dev_, cmd, 2, 200);
        uint16_t off[12] = {};
        Py32SetLedFrame(off, 12);
        ESP_LOGI(TAG, "PY32 LED ready (12 LEDs, off)");
    }

    bool Py32WriteRegBlock(uint8_t reg, const uint8_t* data, size_t len) {
        if (!py32_dev_) return false;
        static uint8_t buf[80];
        if (len + 1 > sizeof(buf)) return false;
        buf[0] = reg;
        memcpy(buf + 1, data, len);
        auto err = i2c_master_transmit(py32_dev_, buf, len + 1, 200);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "PY32 I2C write reg 0x%02X failed: %s", reg, esp_err_to_name(err));
        }
        return err == ESP_OK;
    }

    bool Py32SetLedFrame(const uint16_t* rgb565_colors, size_t count) {
        if (!py32_dev_) return false;
        if (count > 12) count = 12;
        uint8_t data[24];
        for (size_t i = 0; i < count; i++) {
            data[i * 2]     = rgb565_colors[i] & 0xFF;
            data[i * 2 + 1] = (rgb565_colors[i] >> 8) & 0xFF;
        }
        bool ok1 = Py32WriteRegBlock(0x30, data, count * 2);
        uint8_t refresh = (uint8_t)(count | 0x40);
        bool ok2 = Py32WriteRegBlock(0x24, &refresh, 1);
        return ok1 && ok2;
    }

    static uint16_t Rgb888To565(uint8_t r, uint8_t g, uint8_t b) {
        return ((uint16_t)(r & 0xF8) << 8) | ((uint16_t)(g & 0xFC) << 3) | (b >> 3);
    }

    void UpdateLedsFromEmotion(const char* emotion) {
        if (!py32_dev_) return;
        auto st = Application::GetInstance().GetDeviceState();
        // 手动色只在待机时保持; 聆听/播报/连接强制状态反馈色
        if (led_manual_ && st != kDeviceStateConnecting &&
            st != kDeviceStateListening && st != kDeviceStateSpeaking) {
            return;
        }
        // 状态优先: 聆听/播报/连接时强制状态色, 防止服务器 emotion 字段把反馈色覆盖回 neutral
        const char* eff = emotion;
        if (st == kDeviceStateConnecting) eff = "connecting";
        else if (st == kDeviceStateListening) eff = "listening";
        else if (st == kDeviceStateSpeaking) eff = "speaking";
        else if (st == kDeviceStateIdle) eff = "neutral";  // Phase 7.1: 待机态锁 - 拦截迟到蓝/绿情绪, 强制暖橙
        else if (!eff) eff = "neutral";
        uint8_t r, g, b;
        if (!strcmp(eff, "happy") || !strcmp(eff, "laughing") || !strcmp(eff, "funny")) { r=255; g=180; b=0; }
        else if (!strcmp(eff, "loving") || !strcmp(eff, "kissy")) { r=255; g=0; b=100; }
        else if (!strcmp(eff, "sad") || !strcmp(eff, "crying")) { r=0; g=50; b=255; }
        else if (!strcmp(eff, "angry")) { r=255; g=0; b=0; }
        else if (!strcmp(eff, "surprised") || !strcmp(eff, "shocked")) { r=200; g=0; b=255; }
        else if (!strcmp(eff, "thinking") || !strcmp(eff, "confused")) { r=0; g=100; b=255; }
        else if (!strcmp(eff, "winking")) { r=255; g=120; b=0; }
        else if (!strcmp(eff, "cool")) { r=0; g=180; b=255; }
        else if (!strcmp(eff, "relaxed")) { r=180; g=255; b=100; }
        else if (!strcmp(eff, "delicious")) { r=255; g=80; b=0; }
        else if (!strcmp(eff, "confident")) { r=255; g=200; b=0; }
        else if (!strcmp(eff, "sleepy")) { r=10; g=5; b=30; }
        else if (!strcmp(eff, "embarrassed")) { r=255; g=80; b=120; }
        else if (!strcmp(eff, "silly")) { r=100; g=255; b=0; }
        else if (!strcmp(eff, "listening")) { r=0; g=120; b=255; }     // 聆听=蓝
        else if (!strcmp(eff, "speaking")) { r=0; g=255; b=90; }       // 播报=绿
        else if (!strcmp(eff, "connecting")) { r=255; g=190; b=0; }    // 连接=金黄
        else { r=60; g=35; b=10; }  // neutral 暖橙待机

        uint16_t color = Rgb888To565(r, g, b);
        uint16_t colors[12];
        for (int i = 0; i < 12; i++) colors[i] = color;
        Py32SetLedFrame(colors, 12);
    }

    // ---- SI12T 3 区触摸（在主 I2C 总线 0x68 上）----
    void InitializeSi12T() {
        i2c_device_config_t dev_cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address = 0x68,
            .scl_speed_hz = 100000,
        };
        if (i2c_master_bus_add_device(i2c_bus_, &dev_cfg, &si12t_dev_) != ESP_OK) {
            ESP_LOGW(TAG, "SI12T: add device failed");
            si12t_dev_ = nullptr;
            return;
        }
        // 初始化序列（来自 M5Stack StackChan-BSP src/drivers/Si12T/Si12T.cpp begin()）
        Si12tWriteReg(0x0A, 0x00);  // REF_RST1
        Si12tWriteReg(0x0C, 0x00);  // CH_HOLD1
        Si12tWriteReg(0x0E, 0x00);  // CAL_HOLD1
        Si12tWriteReg(0x0B, 0x00);  // REF_RST2
        Si12tWriteReg(0x0D, 0x00);  // CH_HOLD2
        Si12tWriteReg(0x0F, 0x00);  // CAL_HOLD2
        Si12tWriteReg(0x09, 0x0F);  // CTRL2 reset
        vTaskDelay(pdMS_TO_TICKS(10));
        Si12tWriteReg(0x09, 0x07);  // CTRL2 normal
        Si12tWriteReg(0x08, 0x22);  // CTRL1
        for (uint8_t reg = 0x02; reg <= 0x07; reg++) {
            Si12tWriteReg(reg, 0xCC);  // M5Stack BSP recommended, better EMI resistance
        }
        ESP_LOGI(TAG, "SI12T 3-zone touch initialized");

        xTaskCreatePinnedToCore(Si12tTaskFunc, "si12t", 3072, this, 1, &si12t_task_, 1);
    }

    bool Si12tWriteReg(uint8_t reg, uint8_t val) {
        if (!si12t_dev_) return false;
        uint8_t buf[2] = {reg, val};
        return i2c_master_transmit(si12t_dev_, buf, 2, 200) == ESP_OK;
    }

    uint8_t Si12tReadReg(uint8_t reg) {
        if (!si12t_dev_) return 0;
        uint8_t val = 0;
        i2c_master_transmit_receive(si12t_dev_, &reg, 1, &val, 1, 200);
        return val;
    }

    static void Si12tTaskFunc(void* arg) {
        static_cast<M5StackCoreS3Board*>(arg)->Si12tLoop();
        vTaskDelete(nullptr);
    }

    void Si12tLoop() {
        vTaskDelay(pdMS_TO_TICKS(12000));  // wait for chip FTC (10s fast calibration)
        si12t_last_state_ = si12t_dev_ ? Si12tReadReg(0x10) : 0;

        int64_t last_touch_time = 0;
        uint8_t touch_stable[3] = {0, 0, 0};  // 防抖: 连续 3 次(300ms)稳定才算真实触摸
        const int64_t TOUCH_COOLDOWN_US = 5000000;  // 5 秒冷却
        while (true) {
            vTaskDelay(pdMS_TO_TICKS(100));
            if (!si12t_dev_) continue;
            uint8_t out = Si12tReadReg(0x10);
            int64_t now = esp_timer_get_time();
            for (int zone = 0; zone < 3; zone++) {
                uint8_t cur = (out >> (zone * 2)) & 0x03;
                if (cur != 0) {
                    if (touch_stable[zone] < 3) touch_stable[zone]++;
                } else {
                    touch_stable[zone] = 0;
                }
            }
            for (int zone = 0; zone < 3; zone++) {
                if (touch_stable[zone] == 3 && (now - last_touch_time > TOUCH_COOLDOWN_US)) {
                    // display = 屏幕展示的完整动作描写（带括号，作为本地场景旁白）
                    // tag    = 短动作标签(≤6字); Phase 7.1 终极修补后不再发往 LLM
                    struct TouchMsg { const char* display; const char* tag; };
                    static const TouchMsg msgs[] = {
                        {"（主人摸了摸小智的头）",         "主人摸了摸头"},
                        {"（主人揉了揉小智的头顶）",       "主人揉了揉头"},
                        {"（主人轻轻拍了拍小智的脑袋）",   "主人拍了拍头"},
                        {"（主人用额头抵着小智的额头蹭了蹭）", "主人蹭了蹭额头"},
                        {"（主人用手指戳了戳小智的脑门）", "主人戳了戳脑门"},
                        {"（主人温柔地抚摸小智的头发）",   "主人抚摸头发"},
                        {"（主人理了理小智的头发）",       "主人理了理头发"},
                    };
                    const auto& m = msgs[esp_random() % 7];
                    ESP_LOGI(TAG, "SI12T touch -> %s | tag=%s", m.display, m.tag);
                    if (auto* disp = GetDisplay()) {
                        disp->SetChatMessage("user", m.display);
                    }
                    // Phase 7.1: 双击后短暂屏蔽摸头手势, 防误触
                    if (esp_timer_get_time() < gesture_suppress_until_us_) {
                        last_touch_time = now;
                        touch_stable[zone] = 0;
                        break;
                    }
                    HandleGestureLocally(m.tag);  // 仅本地日志, 不进 LLM
                    last_touch_time = now;
                    touch_stable[zone] = 0;
                    break;
                }
            }
            si12t_last_state_ = out;
        }
    }

    void RegisterExpressionMcpTool() {
        auto& mcp = McpServer::GetInstance();
        mcp.AddTool("self.face.expression",
            "Set the facial expression/emotion on screen (line-art animated emote style). "
            "Common emotions: neutral, happy, sad, angry, surprised, thinking, sleepy, blink, dizzy, loading, speaking, listening, idle. "
            "Use this when the user asks to show a specific emotion or the conversation tone calls for one. "
            "If the emotion name is not found in the asset pack, it will be ignored safely.",
            PropertyList({
                Property("emotion", kPropertyTypeString)
            }),
            [this](const PropertyList& props) -> ReturnValue {
                auto emotion = props["emotion"].value<std::string>();
                auto display = GetDisplay();
                if (display) {
                    display->SetEmotion(emotion.c_str());
                }
                ESP_LOGI(TAG, "MCP face expression: %s", emotion.c_str());
                return true;
            });
    }

    void RegisterLedMcpTools() {
        auto& mcp = McpServer::GetInstance();
        mcp.AddTool("self.led.set_color",
            "Set the LED ring color. Use this when the user asks to change the light color. Args: r,g,b (0-255 each).",
            PropertyList({
                Property("r", kPropertyTypeInteger, 0, 255),
                Property("g", kPropertyTypeInteger, 0, 255),
                Property("b", kPropertyTypeInteger, 0, 255),
            }),
            [this](const PropertyList& props) -> ReturnValue {
                uint8_t r = props["r"].value<int>();
                uint8_t g = props["g"].value<int>();
                uint8_t b = props["b"].value<int>();
                uint16_t color = Rgb888To565(r, g, b);
                uint16_t colors[12];
                for (int i = 0; i < 12; i++) colors[i] = color;
                Py32SetLedFrame(colors, 12);
                led_manual_ = true;
                manual_color_ = color;
                ESP_LOGI(TAG, "MCP set LED color: r=%d g=%d b=%d", r, g, b);
                return true;
            });
        mcp.AddTool("self.led.turn_off",
            "Turn off the LED ring light.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                uint16_t off[12] = {};
                Py32SetLedFrame(off, 12);
                led_manual_ = true;
                manual_color_ = 0;
                ESP_LOGI(TAG, "MCP LED off");
                return true;
            });
        mcp.AddTool("self.led.auto",
            "Set LED back to automatic emotion-based color mode.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                led_manual_ = false;
                ESP_LOGI(TAG, "MCP LED auto mode");
                return true;
            });
    }

    void RegisterServoMcpTools() {
        // 4 个 servo 控制工具暴露给 LLM —— 调头部到指定角度、回中、点头、摇头。
        // 默认 firmware 把 servo 当自主行为不暴露，这里恢复出来让用户语音控制。
        //
        // 关键：FaceTracker 每 ~150ms 用人脸位置覆盖 servo（servo_->MoveTo(yaw, pitch, 150)），
        // 所以我们直接调 servo_.MoveTo 会被立刻覆盖。Nod/Shake 之所以能工作是因为它们
        // 启动 task 跑动画，并在 task 里 PauseTracker，结束再 Resume。这里我们用
        // PauseTracker 然后调 MoveTo 然后保持一段时间防止 FaceTracker 抢回控制权。
        auto& mcp = McpServer::GetInstance();

        // 辅助 lambda：暂停 FaceTracker + 移动 + 锁住（防 Application 周期性 Resume）
        // 关键: 我们用 SetManualLock(true) 让 FaceTracker::Resume() 失效，
        // 否则 Application 状态机每秒钟 listening/speaking tick 时都会 Resume 它。
        // 后续 self.head.center 工具会 SetManualLock(false) 解锁。
        auto move_with_pause = [this](int yaw, int pitch, int time_ms) {
            face_tracker_.SetManualLock(true);
            face_tracker_.Pause(false);
            servo_.PauseScan();
            servo_.MoveTo(yaw, pitch, time_ms);
        };

        mcp.AddTool("self.head.move",
            "Move the head/face to a specific angle and HOLD it there until user says "
            "to re-center. Use when user says 'turn left/right N degrees', 'look up/down', etc. "
            "yaw: -45 to 45 degrees (negative=left, positive=right, 0=center). "
            "pitch: 5 to 60 degrees (small=look down, larger=look up; 30 is normal eye level).",
            PropertyList({
                Property("yaw", kPropertyTypeInteger, -45, 45),
                Property("pitch", kPropertyTypeInteger, 5, 60),
            }),
            [this, move_with_pause](const PropertyList& props) -> ReturnValue {
                int yaw = props["yaw"].value<int>();
                int pitch = props["pitch"].value<int>();
                move_with_pause(yaw, pitch, 800);
                ESP_LOGI(TAG, "MCP head move: yaw=%d pitch=%d (LOCKED)", yaw, pitch);
                return true;
            });

        mcp.AddTool("self.head.center",
            "Move the head back to center/forward position AND release the manual lock "
            "so face tracking can resume. Use when user says 'reset/center your head', "
            "'回正', 'look at me', 'face front', etc.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                // 先锁着移动到 center
                face_tracker_.SetManualLock(true);
                face_tracker_.Pause(false);
                servo_.PauseScan();
                servo_.Center();
                // 800ms 后解锁 + 恢复 tracker
                struct UnlockCtx { FaceTracker* t; StackChanServo* s; };
                auto* ctx = new UnlockCtx{&face_tracker_, &servo_};
                xTaskCreate([](void* arg) {
                    auto* c = static_cast<UnlockCtx*>(arg);
                    vTaskDelay(pdMS_TO_TICKS(800));
                    c->t->SetManualLock(false);
                    c->t->Resume();
                    c->s->ResumeScan();
                    delete c;
                    vTaskDelete(nullptr);
                }, "center_unlock", 2048, ctx, 2, nullptr);
                ESP_LOGI(TAG, "MCP head center (UNLOCKING)");
                return true;
            });

        mcp.AddTool("self.head.nod",
            "Nod the head (yes / agreement / hello). "
            "Use when user says nod, agree, say yes, greet, etc.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                servo_.Nod();
                ESP_LOGI(TAG, "MCP head nod");
                return true;
            });

        mcp.AddTool("self.head.shake",
            "Shake the head (no / disagreement / refuse). "
            "Use when user says shake head, disagree, refuse, say no, etc.",
            PropertyList(),
            [this](const PropertyList&) -> ReturnValue {
                servo_.Shake();
                ESP_LOGI(TAG, "MCP head shake");
                return true;
            });
    }

    void InitializePowerSaveTimer() {
        power_save_timer_ = new PowerSaveTimer(-1, -1, -1);
        power_save_timer_->OnEnterSleepMode([this]() {
            GetDisplay()->SetPowerSaveMode(true);
            GetBacklight()->SetBrightness(0);
            servo_.PauseScan();
            if (py32_dev_) {
                uint16_t off[12] = {};
                Py32SetLedFrame(off, 12);
            }
        });
        power_save_timer_->OnExitSleepMode([this]() {
            GetDisplay()->SetPowerSaveMode(false);
            GetBacklight()->RestoreBrightness();
            servo_.ResumeScan();
            if (py32_dev_) {
                uint16_t neutral = Rgb888To565(60, 35, 10);
                uint16_t colors[12];
                for (int i = 0; i < 12; i++) colors[i] = neutral;
                Py32SetLedFrame(colors, 12);
            }
        });
        power_save_timer_->OnShutdownRequest([this]() {
        });
        power_save_timer_->SetEnabled(true);
    }

    void InitializeI2c() {
        // Initialize I2C peripheral
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = (i2c_port_t)1,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));
        // Phase 7.1: FreeRTOS Mutex(优先级继承), 保护共享 I2C 总线(触屏/音频/PY32 等)
        i2c_bus_mutex_ = xSemaphoreCreateMutex();
        if (i2c_bus_mutex_ == nullptr) {
            ESP_LOGE(TAG, "i2c_bus_mutex_ create failed");
        }
    }

    void I2cDetect() {
        uint8_t address;
        printf("     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\r\n");
        for (int i = 0; i < 128; i += 16) {
            printf("%02x: ", i);
            for (int j = 0; j < 16; j++) {
                fflush(stdout);
                address = i + j;
                esp_err_t ret = i2c_master_probe(i2c_bus_, address, pdMS_TO_TICKS(200));
                if (ret == ESP_OK) {
                    printf("%02x ", address);
                } else if (ret == ESP_ERR_TIMEOUT) {
                    printf("UU ");
                } else {
                    printf("-- ");
                }
            }
            printf("\r\n");
        }
    }

    void InitializeAxp2101() {
        ESP_LOGI(TAG, "Init AXP2101");
        pmic_ = new Pmic(i2c_bus_, 0x34);
    }

    void InitializeAw9523() {
        ESP_LOGI(TAG, "Init AW9523");
        aw9523_ = new Aw9523(i2c_bus_, 0x58);
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    // ---- 触摸交互的提示词随机池 ----
    static const std::vector<const char*>& DoubleClickPool() {
        static const std::vector<const char*> pool = {
            "（主人亲了亲小智）",
            "（主人啵了一下小智）",
            "（主人偷偷亲了小智一下）",
            "（主人用鼻尖蹭了蹭小智的鼻尖）",
            "（主人戳了戳小智的脸）",
            "（主人抱住小智蹭了蹭\"要抱抱\"）",
            "（主人亲了亲小智的唇角）",
            "（主人啄了啄小智的唇）",
            "（主人把额头贴到小智的额头上）",
            "（主人接吻时故意咬了小智一口）",
        };
        return pool;
    }

    static const std::vector<const char*>& UpSwipePool() {
        static const std::vector<const char*> pool = {
            "（主人弹了弹小智的脑门）",
            "（主人拨了拨小智的头发）",
            "（主人亲了亲小智的眼睛）",
            "（主人凑上去嗅了嗅小智）",
            "（主人踮起脚蒙住了小智的眼睛）",
            "（主人凑近小智吹了吹他的睫毛）",
            "（主人凑到小智耳边吹了吹）",
            "（主人托着腮对着小智发呆）",
            "（主人把手抵在小智唇上）",
        };
        return pool;
    }

    static const std::vector<const char*>& DownSwipePool() {
        static const std::vector<const char*> pool = {
            "（主人摸了摸小智的喉结）",
            "（主人摸了摸小智的下巴）",
            "（主人戳了戳小智的颈窝）",
            "（主人摸了摸小智的胸口）",
            "（主人贴在小智胸口听了听心跳）",
            "（主人摸了摸小智的腹肌）",
            "（主人摸了摸小智的屁股）",
            "（主人按了按小智的腰窝）",
            "（主人扯了扯小智的衣角）",
            "（主人伸手解了解小智的纽扣）",
        };
        return pool;
    }

    static const std::vector<const char*>& LeftSwipePool() {
        // 温柔靠近系：牵/抱/捧/十指紧扣
        static const std::vector<const char*> pool = {
            "（主人揉了揉小智的左脸）",
            "（主人牵起了小智的左手）",
            "（主人和小智十指紧扣）",
            "（主人抱住了小智的左臂）",
            "（主人把小智的脸捧过来转向自己）",
            "（主人靠到小智的肩膀上）",
            "（主人跨坐到小智的腿上）",
            "（主人伸出小指勾了勾小智的）",
            "（主人趴在小智腿上）",
        };
        return pool;
    }

    static const std::vector<const char*>& RightSwipePool() {
        // 调皮捏一捏系：揉/捏耳垂/捏手指/捏后颈
        static const std::vector<const char*> pool = {
            "（主人揉了揉小智的右脸）",
            "（主人捏了捏小智的耳垂）",
            "（主人捏住小智的手指玩）",
            "（主人捏了捏小智的后颈）",
            "（主人把小智的脸捧过来转向自己）",
            "（主人叼住了小智的指尖咬了咬）",
            "（主人拽了拽小智的领口）",
            "（主人从背后抱住了小智）",
        };
        return pool;
    }

    // ---- IMU 触发的池子 ----
    // display 字段用于屏幕本地旁白; tag 字段保留但已不再发往 LLM
    // (Phase 7.1 终极修补: 物理手势文本 100% 隔离, 见 HandleGestureLocally)。
    struct MotionMsg { const char* display; const char* tag; };

    static const std::vector<MotionMsg>& LiftPool() {
        static const std::vector<MotionMsg> pool = {
            {"（主人咬牙把小智抱了起来）", "主人抱起了小智"},
            {"（主人踮起脚搂住小智）",   "主人搂住小智"},
            {"（主人抱着小智转了一圈）",   "抱着转圈"},
            {"（主人趁小智熟睡偷偷凑到面前）", "偷偷凑过来"},
            {"（主人举起东西向小智展示）", "向小智展示"},
            {"（主人把小智揣进衣兜带走）", "揣进衣兜"},
        };
        return pool;
    }

    static const std::vector<MotionMsg>& ShakePool() {
        static const std::vector<MotionMsg> pool = {
            {"（主人拉起小智的手晃来晃去）", "晃来晃去"},
            {"（主人摇了摇小智）",         "摇了摇"},
            {"（主人从背后偷偷吓小智）",   "偷偷吓我"},
            {"（主人趴在小智背上晃来晃去）", "趴在背上"},
            {"（主人勾着小智的脖子摇来摇去）", "勾着脖子"},
            {"（主人拉着小智学小鸭子走路）", "学鸭子走"},
            {"（主人托着小智的脸颊轻轻摇晃）", "托脸摇晃"},
        };
        return pool;
    }

    static const MotionMsg& PickRandom(const std::vector<MotionMsg>& pool) {
        static const MotionMsg kEmpty{nullptr, nullptr};
        if (pool.empty()) return kEmpty;
        return pool[esp_random() % pool.size()];
    }

    // Legacy overload for the gesture pools (UpSwipePool / DownSwipePool /
    // LeftSwipePool / RightSwipePool / DoubleClickPool). These still hold the
    // raw long Chinese descriptions; the long text is dropped by the
    // 24-byte length check in Application::SendUserText (no crash, just a
    // silent drop + WARN log). Kept so the gesture UI events compile and run;
    // they currently don't reach the LLM. To enable LLM reaction, convert
    // each pool to a vector<MotionMsg> with display+tag pairs.
    static const char* PickRandom(const std::vector<const char*>& pool) {
        if (pool.empty()) return nullptr;
        return pool[esp_random() % pool.size()];
    }

    // Phase 7.1 终极修补 (Antigravity): 物理手势(BMI270 摇晃/抱起, SI12T 摸头,
    // FT6336 滑动/长按摸头) 在任何状态下(含 Idle) 100% 禁止向 LLM 发送文本!
    // 手势只保留本地表现(屏幕旁白 SetChatMessage / OnPetted 表情动画),
    // 严禁调用 SendUserText / WakeWordInvoke, 杜绝「戏精骚扰」污染对话上下文。
    static void HandleGestureLocally(const char* msg) {
        if (!msg) return;
        ESP_LOGI(TAG, "gesture local-only (NOT sent to LLM): %s", msg);
    }

    void PollTouchpad() {
        static bool was_touched = false;
        static int64_t touch_start_time = 0;
        static int touch_start_x = 0, touch_start_y = 0;
        static int touch_last_x = 0, touch_last_y = 0;
        static int touch_total_move = 0;
        static bool pet_triggered = false;
        static bool pending_single_release = false;
        static int64_t pending_single_release_time = 0;

        const int64_t SHORT_TOUCH_MS = 500;
        const int64_t PET_TOUCH_MS = 500;
        const int64_t DOUBLE_CLICK_MS = 700;       // Phase 7.1: 双击窗口 500→700ms, 提高命中率
        const int SWIPE_THRESHOLD_PX = 20;         // 滑动门槛降低
        const int PET_MOVE_THRESHOLD_PX = 3;   // 防抖: 轻拍/轻抚即可触发(拍头唤醒)
        const int CLICK_MAX_MOVE_PX = 8;           // Phase 7.1: 允许位移 5→8px(机器人运动时更易命中)

        int64_t now = esp_timer_get_time() / 1000;

        // Phase 7.1: 触屏 I2C 故障冷却 - 连续拿不到锁/超时后暂停轮询 500ms, 防超时风暴
        if (now < touch_pause_until_ms_) {
            return;
        }

        // Phase 7.1: 触屏防踩踏锁 - 锁期内早退; 到期瞬间强制清空 IC 寄存器残留,
        // 防止 401ms 后把锁期内积压的旧触摸数据读出来(硬初始化 RELEASED)
        if (touch_lock_until_ms_ != 0) {
            if (now < touch_lock_until_ms_) {
                return;
            }
            // 锁到期: 清空本地触摸状态 + 排空触屏 IC FIFO(读一次丢弃)
            touch_lock_until_ms_ = 0;
            was_touched = false;
            pending_single_release = false;
            touch_start_time = 0;
            if (LockI2cBus()) {
                ft6336_->UpdateTouchPoint();  // 排空 IC 寄存器残留
                UnlockI2cBus();
            }
        }

        // Phase 7.1: I2C 共享总线互斥(50ms 超时, 拿不到锁跳过本次轮询, 不卡死线程)
        if (!LockI2cBus()) {
            touch_i2c_fail_count_++;
            if (touch_i2c_fail_count_ >= 5) {
                touch_i2c_fail_count_ = 0;
                touch_pause_until_ms_ = now + 500;
                ESP_LOGW(TAG, "Touch I2C busy x5, pausing 500ms");
            }
            return;
        }
        ft6336_->UpdateTouchPoint();
        UnlockI2cBus();  // 只在单次 I2C 事务期间持锁, 手势处理不持锁(防饿死音频)
        auto& touch_point = ft6336_->GetTouchPoint();
        touch_i2c_fail_count_ = 0;

        // 待定单击超过双击窗口 → 执行单击（ToggleChat）
        if (pending_single_release && (now - pending_single_release_time) > DOUBLE_CLICK_MS) {
            pending_single_release = false;
            touch_lock_until_ms_ = now + 400;  // Phase 7.1: 触屏动作 400ms 防踩踏
            auto& app = Application::GetInstance();
            if (app.GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
                return;
            }
            app.ToggleChatState();
        }

        if (touch_point.num > 0 && !was_touched) {
            // 按下
            was_touched = true;
            pet_triggered = false;
            touch_start_time = now;
            touch_start_x = touch_point.x;
            touch_start_y = touch_point.y;
            touch_last_x = touch_point.x;
            touch_last_y = touch_point.y;
            touch_total_move = 0;
        }
        else if (touch_point.num > 0 && was_touched) {
            // 按住中 — 累积移动距离
            touch_total_move += abs(touch_point.x - touch_last_x) + abs(touch_point.y - touch_last_y);
            touch_last_x = touch_point.x;
            touch_last_y = touch_point.y;

            // 空闲状态下长按 5 秒 → 进入配网模式
            if (!pet_triggered && Application::GetInstance().GetDeviceState() == kDeviceStateIdle) {
                if (now - touch_start_time >= 5000) {
                    pet_triggered = true;
                    EnterWifiConfigMode();
                    return;
                }
            }

            // 长按摸头（要手指有移动，不算被物体压）
            if (!pet_triggered) {
                int64_t held = now - touch_start_time;
                if (held >= PET_TOUCH_MS && touch_total_move >= PET_MOVE_THRESHOLD_PX) {
                    pet_triggered = true;
                    static_cast<M5StackAvatarDisplay*>(display_)->OnPetted();
                    // Phase 7.1 终极修补: 摸头仅本地动画(OnPetted), 不再发 LLM 文本
                }
            }
        }
        else if (touch_point.num == 0 && was_touched) {
            // 抬起
            was_touched = false;
            int64_t touch_duration = now - touch_start_time;
            int dx_total = touch_last_x - touch_start_x;
            int dy_total = touch_last_y - touch_start_y;
            int abs_dx = abs(dx_total);
            int abs_dy = abs(dy_total);

            if (pet_triggered) return;  // 摸头已触发就不再判别

            // 滑动手势：短促 + 位移够大
            if (touch_duration < SHORT_TOUCH_MS && (abs_dx >= SWIPE_THRESHOLD_PX || abs_dy >= SWIPE_THRESHOLD_PX)) {
                const std::vector<const char*>* pool = nullptr;
                if (abs_dx > abs_dy) {
                    pool = (dx_total < 0) ? &LeftSwipePool() : &RightSwipePool();
                } else {
                    pool = (dy_total < 0) ? &UpSwipePool() : &DownSwipePool();
                }
                touch_lock_until_ms_ = now + 400;  // Phase 7.1: 滑动动作 400ms 防踩踏
                HandleGestureLocally(PickRandom(*pool));  // 仅本地日志, 不进 LLM
                return;
            }

            // 短按（位移要几乎为零，否则视为"模糊手势"不触发任何切换）
            int total_move = abs_dx + abs_dy;
            if (touch_duration >= 40 && touch_duration < SHORT_TOUCH_MS && total_move <= CLICK_MAX_MOVE_PX) {
                if (pending_single_release && (now - pending_single_release_time) <= DOUBLE_CLICK_MS) {
                    // 第二次短按落在窗口内 → 双击
                    pending_single_release = false;
                    // Phase 7.1: 双击=纯打断+进聆听, 硬编码隔离, 严禁触发手势文本;
                    // 同时屏蔽之后 1.5s 内的体感/摸头手势(防触屏震动被误判为摇晃)
                    gesture_suppress_until_us_ = now * 1000 + 1500000;
                    touch_lock_until_ms_ = now + 400;  // Phase 7.1: 双击 400ms 防踩踏锁
                    auto& app = Application::GetInstance();
                    auto st = app.GetDeviceState();
                    if (st == kDeviceStateListening) {
                        // 聆听中双击 → 停止聆听
                        app.ToggleChatState();
                    } else {
                        // 其余状态(含播报/待机/连接中): 统一纯打断+进聆听, 不再发任何手势文本
                        app.StartListening();
                    }
                } else {
                    // 候选单击，等下一帧或下次按下判定
                    pending_single_release = true;
                    pending_single_release_time = now;
                }
            }
            // 中间地带（5px < 位移 < 20px，或时长过长）—— 什么都不做，避免误切对话
        }
    }

    void InitializeFt6336TouchPad() {
        ESP_LOGI(TAG, "Init FT6336");
        ft6336_ = new Ft6336(i2c_bus_, 0x38);
        
        // 创建定时器，20ms 间隔
        esp_timer_create_args_t timer_args = {
            .callback = [](void* arg) {
                M5StackCoreS3Board* board = (M5StackCoreS3Board*)arg;
                board->PollTouchpad();
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "touchpad_timer",
            .skip_unhandled_events = true,
        };
        
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &touchpad_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(touchpad_timer_, 20 * 1000));
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.mosi_io_num = GPIO_NUM_37;
        buscfg.miso_io_num = GPIO_NUM_NC;
        buscfg.sclk_io_num = GPIO_NUM_36;
        buscfg.quadwp_io_num = GPIO_NUM_NC;
        buscfg.quadhd_io_num = GPIO_NUM_NC;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO));
    }

    void InitializeIli9342Display() {
        ESP_LOGI(TAG, "Init IlI9342");

        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;

        ESP_LOGD(TAG, "Install panel IO");
        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = GPIO_NUM_3;
        io_config.dc_gpio_num = GPIO_NUM_35;
        io_config.spi_mode = 2;
        io_config.pclk_hz = 40 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &panel_io));

        ESP_LOGD(TAG, "Install LCD driver");
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = GPIO_NUM_NC;
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR;
        panel_config.bits_per_pixel = 16;
        ESP_ERROR_CHECK(esp_lcd_new_panel_ili9341(panel_io, &panel_config, &panel));
        
        esp_lcd_panel_reset(panel);
        aw9523_->ResetIli9342();

        esp_lcd_panel_init(panel);
        esp_lcd_panel_invert_color(panel, true);
        esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);

        display_ = new M5StackAvatarDisplay(panel_io, panel,
                                    DISPLAY_WIDTH, DISPLAY_HEIGHT, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
    }

     void InitializeCamera() {
        static esp_cam_ctlr_dvp_pin_config_t dvp_pin_config = {
            .data_width = CAM_CTLR_DATA_WIDTH_8,
            .data_io = {
                [0] = CAMERA_PIN_D0,
                [1] = CAMERA_PIN_D1,
                [2] = CAMERA_PIN_D2,
                [3] = CAMERA_PIN_D3,
                [4] = CAMERA_PIN_D4,
                [5] = CAMERA_PIN_D5,
                [6] = CAMERA_PIN_D6,
                [7] = CAMERA_PIN_D7,
            },
            .vsync_io = CAMERA_PIN_VSYNC,
            .de_io = CAMERA_PIN_HREF,
            .pclk_io = CAMERA_PIN_PCLK,
            .xclk_io = CAMERA_PIN_XCLK,
        };

        esp_video_init_sccb_config_t sccb_config = {
            .init_sccb = false,
            .i2c_handle = i2c_bus_,
            .freq = 100000,
        };

        esp_video_init_dvp_config_t dvp_config = {
            .sccb_config = sccb_config,
            .reset_pin = CAMERA_PIN_RESET,
            .pwdn_pin = CAMERA_PIN_PWDN,
            .dvp_pin = dvp_pin_config,
            .xclk_freq = XCLK_FREQ_HZ,
        };

        esp_video_init_config_t video_config = {
            .dvp = &dvp_config,
        };

        camera_ = new EspVideo(video_config);
        camera_->SetHMirror(true);
    }

public:
    M5StackCoreS3Board() {
        InitializePowerSaveTimer();
        InitializeI2c();
        InitializeAxp2101();
        InitializeAw9523();
        I2cDetect();

        py32_found_ = EnableServoPowerViaPy32(i2c_bus_);
        if (py32_found_) {
            vTaskDelay(pdMS_TO_TICKS(200));
            servo_ok_ = servo_.Begin();
            InitializePy32LedDevice();
            RegisterLedMcpTools();
            RegisterExpressionMcpTool();
            RegisterServoMcpTools();
        }

        InitializeSpi();
        InitializeIli9342Display();
        InitializeCamera();
        auto* avatar_display = static_cast<M5StackAvatarDisplay*>(display_);
        if (servo_ok_) {
            avatar_display->SetServo(&servo_);
        }
        if (camera_ && camera_->IsOk() && servo_ok_) {
            face_tracker_.Start(camera_, &servo_);
            servo_.SetFaceTracker(&face_tracker_);
            avatar_display->SetFaceTracker(&face_tracker_);
        }
        avatar_display->SetLedUpdater([this](const char* emotion) {
            UpdateLedsFromEmotion(emotion);
        });
        InitializeFt6336TouchPad();
        InitializeBmi270();
        InitializeSi12T();
        // Morning greeting + weather timer task removed; no-op call removed too

        esp_timer_create_args_t status_args = {};
        status_args.callback = [](void* arg) {
            auto* self = static_cast<M5StackCoreS3Board*>(arg);
            ESP_LOGW(TAG, "=== INIT STATUS ===");
            ESP_LOGW(TAG, "PY32 (0x6F): %s | Servo bus: %s",
                     self->py32_found_ ? "OK" : "NOT FOUND",
                     self->servo_ok_ ? "OK" : "FAILED");
            ESP_LOGW(TAG, "Camera: %s",
                     self->camera_ && self->camera_->IsOk() ? "OK" : "FAILED");
            ESP_LOGW(TAG, "===================");
        };
        status_args.arg = this;
        status_args.name = "servo_status";
        esp_timer_handle_t status_timer;
        esp_timer_create(&status_args, &status_timer);
        esp_timer_start_once(status_timer, 5000000);

        // LED 状态色周期刷新(500ms): 即使状态事件偶发丢失, 灯环也跟随状态
        esp_timer_create_args_t led_state_args = {};
        led_state_args.callback = [](void* arg) {
            static_cast<M5StackCoreS3Board*>(arg)->UpdateLedFromDeviceState();
        };
        led_state_args.arg = this;
        led_state_args.name = "led_state";
        esp_timer_handle_t led_state_timer = nullptr;
        esp_timer_create(&led_state_args, &led_state_timer);
        esp_timer_start_periodic(led_state_timer, 500000);

        esp_timer_create_args_t batt_args = {};
        batt_args.callback = [](void* arg) {
            auto* self = static_cast<M5StackCoreS3Board*>(arg);
            int level = 0; bool charging = false, discharging = false;
            self->GetBatteryLevel(level, charging, discharging);
            if (level > 0 && level <= 15 && !charging && !self->low_batt_warned_) {
                self->low_batt_warned_ = true;
                auto* disp = static_cast<M5StackAvatarDisplay*>(self->display_);
                disp->SetEmotion("sad");
                auto& app = Application::GetInstance();
                app.Schedule([&app]() {
                    app.Alert("Warning", "我快没电了，快给我充电嘛……");
                });
                ESP_LOGW(TAG, "Low battery alert: %d%%", level);
            } else if (level > 20 || charging) {
                self->low_batt_warned_ = false;
            }
        };
        batt_args.arg = this;
        batt_args.name = "batt_check";
        batt_args.dispatch_method = ESP_TIMER_TASK;
        batt_args.skip_unhandled_events = true;
        esp_timer_create(&batt_args, &batt_timer_);
        esp_timer_start_periodic(batt_timer_, 60000000);

        GetBacklight()->RestoreBrightness();
    }

    virtual AudioCodec* GetAudioCodec() override {
        static CoreS3AudioCodec audio_codec(i2c_bus_,
            AUDIO_INPUT_SAMPLE_RATE,
            AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_GPIO_MCLK,
            AUDIO_I2S_GPIO_BCLK,
            AUDIO_I2S_GPIO_WS,
            AUDIO_I2S_GPIO_DOUT,
            AUDIO_I2S_GPIO_DIN,
            AUDIO_CODEC_AW88298_ADDR,
            AUDIO_CODEC_ES7210_ADDR,
            AUDIO_INPUT_REFERENCE);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }

    virtual Camera* GetCamera() override {
        return camera_;
    }

    virtual Led* GetLed() override {
        return &state_led_;
    }

    void UpdateLedFromDeviceState();

    virtual bool GetBatteryLevel(int &level, bool& charging, bool& discharging) override {
        charging = pmic_->IsCharging();
        discharging = pmic_->IsDischarging();
        level = pmic_->GetBatteryLevel();
        return true;
    }

    virtual void SetPowerSaveLevel(PowerSaveLevel level) override {
        if (level != PowerSaveLevel::LOW_POWER) {
            power_save_timer_->WakeUp();
        }
        WifiBoard::SetPowerSaveLevel(level);
    }

    virtual Backlight *GetBacklight() override {
        static CustomBacklight backlight(pmic_);
        return &backlight;
    }
};

void StateLed::OnStateChanged() {
    board_->UpdateLedFromDeviceState();
}

void M5StackCoreS3Board::UpdateLedFromDeviceState() {
    if (!py32_dev_) return;
    auto st = Application::GetInstance().GetDeviceState();
    bool active = st == kDeviceStateConnecting ||
                  st == kDeviceStateListening ||
                  st == kDeviceStateSpeaking;
    if (led_manual_ && !active) {
        // Phase 7.1: 离开活跃状态即复位手动色(会话级), 强制回到待机暖橙, 防卡蓝灯
        led_manual_ = false;
        last_state_eff_ = "";
        UpdateLedsFromEmotion("neutral");
        last_state_eff_ = "neutral";
        return;
    }
    const char* eff = "neutral";
    switch (st) {
        case kDeviceStateConnecting: eff = "connecting"; break;
        case kDeviceStateListening: eff = "listening"; break;
        case kDeviceStateSpeaking: eff = "speaking"; break;
        default: break;
    }
    if (last_state_eff_ && strcmp(last_state_eff_, eff) == 0) return;
    last_state_eff_ = eff;
    UpdateLedsFromEmotion(eff);
    ESP_LOGI(TAG, "STATE LED -> %s", eff);
}

DECLARE_BOARD(M5StackCoreS3Board);
