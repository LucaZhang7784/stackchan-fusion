#pragma once
#include "sdkconfig.h"

#include <lvgl.h>
#include <thread>
#include <memory>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "camera.h"
#include "jpg/image_to_jpeg.h"
#include "esp_video_init.h"

struct JpegChunk {
    uint8_t* data;
    size_t len;
};

class EspVideo : public Camera {
private:
    struct FrameBuffer {
        uint8_t *data = nullptr;
        size_t len = 0;
        uint16_t width = 0;
        uint16_t height = 0;
        v4l2_pix_fmt_t format = 0;
    } frame_;
    v4l2_pix_fmt_t sensor_format_ = 0;
#ifdef CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    uint16_t sensor_width_ = 0;
    uint16_t sensor_height_ = 0;
#endif  // CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    int video_fd_ = -1;
    bool streaming_on_ = false;
    struct MmapBuffer { void *start = nullptr; size_t length = 0; };
    std::vector<MmapBuffer> mmap_buffers_;
    std::string explain_url_;
    std::string explain_token_;
    std::thread encoder_thread_;

public:
    EspVideo(const esp_video_init_config_t& config);
    ~EspVideo();

    bool IsOk() const { return streaming_on_ && video_fd_ >= 0; }

    const uint8_t* FrameData() const { return frame_.data; }
    size_t FrameLen() const { return frame_.len; }
    uint16_t FrameWidth() const { return frame_.width; }
    uint16_t FrameHeight() const { return frame_.height; }
    v4l2_pix_fmt_t FrameFormat() const { return frame_.format; }

    uint16_t SensorWidth() const { return frame_.width; }
    uint16_t SensorHeight() const { return frame_.height; }
    v4l2_pix_fmt_t SensorFormat() const { return sensor_format_; }

    template<typename Fn>
    bool PeekFrame(Fn callback) {
        if (!streaming_on_ || video_fd_ < 0) return false;
        struct v4l2_buffer buf = {};
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        if (ioctl(video_fd_, VIDIOC_DQBUF, &buf) != 0) return false;
        callback(static_cast<const uint8_t*>(mmap_buffers_[buf.index].start),
                 mmap_buffers_[buf.index].length, frame_.width, frame_.height);
        ioctl(video_fd_, VIDIOC_QBUF, &buf);
        return true;
    }
    virtual void SetExplainUrl(const std::string& url, const std::string& token);
    virtual bool Capture();
    // 翻转控制函数
    virtual bool SetHMirror(bool enabled) override;
    virtual bool SetVFlip(bool enabled) override;
    virtual std::string Explain(const std::string& question);
};
