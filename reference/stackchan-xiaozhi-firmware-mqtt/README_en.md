# 🤖 Stackchan-XiaoZhi

> M5Stack Core S3 Stack-chan Companion Robot Firmware — Touch · Motion · Emotion LEDs · Servos

Built on [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32), focused on the **companionship** experience. Derived from [mo-hantang/Stackchan-HtSz](https://github.com/mo-hantang/Stackchan-HtSz).

<p align="center">
  <img src="https://img.shields.io/badge/platform-ESP32--S3-green?logo=espressif" alt="ESP32-S3">
  <img src="https://img.shields.io/badge/framework-ESP--IDF%20v5.5-blue?logo=espressif" alt="ESP-IDF v5.5">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="License">
  <img src="https://img.shields.io/github/v/release/howecheung/Stackchan-XiaoZhi" alt="Release">
</p>

---

## ✨ Features

| Module | Hardware | Description |
|---|---|---|
| 🎤 Wake Word | Multinet6 | "你好小智", sensitivity Low / Medium / High |
| 👆 Head Touch | SI12T | 3-zone capacitive touch, head pat to talk, 7 random responses |
| 📱 Screen Touch | FT6336 | 6 gestures (double-tap / swipe / long-press), 60 action texts |
| 🕺 Motion Sense | BMI270 | Shake + lift interaction, auto-mute during speech, 5 min cooldown |
| 💡 Emotion LED Ring | WS2812×12 | 21 emotion colors, controlled via PY32 IO Expander |
| 🎯 Servo Tracking | SCS Bus | GC0308 face tracking + idle scanning, pauses during conversation |
| 😴 Smart Sleep | — | 30s idle: lights off / servos off / screen off, wake word to resume |
| 🔌 Reconfigure Wi-Fi | — | Idle long-press screen 5s to enter 192.168.4.1 config page (redesigned UI, upgraded to esp-wifi-connect v3.2.1, dark mode) |
| ✂️ Text Interruption | — | Inject text messages during conversation without silent discard |
| 🔧 MCP Tools | — | LED / Volume / Brightness / Camera / System Info |

---

## 🚀 Quick Start

Download `bootloader.bin`, `partition-table.bin`, `ota_data_initial.bin`, `xiaozhi.bin` from [Releases](https://github.com/howecheung/Stackchan-XiaoZhi/releases), flash with a single command:

```bash
pip install esptool
esptool.py --chip esp32s3 -p COM4 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash --flash_mode dio --flash_size 16MB --flash_freq 80m \
  0x0       bootloader.bin \
  0x8000    partition-table.bin \
  0xd000    ota_data_initial.bin \
  0x410000  xiaozhi.bin
```

> Replace `COM4` with your actual serial port. For first-time flashing, run `esptool.py erase_flash` first to clear old config.

---

## 🔨 Build from Source

Requires **ESP-IDF v5.5.x**. Run in "ESP-IDF 5.5 CMD":

```bat
git clone https://github.com/howecheung/Stackchan-XiaoZhi.git
cd Stackchan-XiaoZhi

:: First build
idf.py set-target esp32s3
idf.py fullclean
idf.py build flash

:: Subsequent incremental builds
idf.py build flash
```

For low-spec machines to avoid OOM: `idf.py build flash -- -j1`

---

## ⚙️ Configuration

Edit `sdkconfig.defaults`:

| Config | Default | Description |
|---|---|---|
| `CONFIG_CUSTOM_WAKE_WORD` | `ni hao xiao zhi` | Wake word pinyin |
| `CONFIG_CUSTOM_WAKE_WORD_DISPLAY` | `你好小智` | Chinese display |
| `CONFIG_WAKE_WORD_SENSITIVITY` | `MEDIUM` | `LOW` / `MEDIUM` / `HIGH` |

### 🛠 Custom Behavior Pools

Edit `main/boards/m5stack-core-s3/m5stack_core_s3.cc`:

| Search Keyword | Function |
|---|---|
| `HeadTouchPool` | Head pat response texts |
| `DoubleClickPool` / `UpSwipePool` … | Gesture action texts |
| `ShakePool` / `LiftPool` | Motion actions (MotionMsg format) |
| `UpdateLedsFromEmotion` | Emotion LED color mapping |

---

## 📖 Usage

### 🔌 WiFi Config Page Preview (No Flashing Required)

Preview the WiFi config page locally without compiling or flashing:

```bash
node scripts/wifi-preview.js
```

Open `http://localhost:3000` in browser. Edit `managed_components/78__esp-wifi-connect/assets/wifi_configuration.html` and refresh to see changes. Mock data includes 5 WiFi hotspots, 3 saved networks, and visible OTA/sleep options.

---

## ⚠️ Known Issues

<details>
<summary>Click to expand</summary>

1. **Battery power + all LEDs on may fail to boot** — Plug in USB first, then power on
2. **LLM LED control occasionally stuck / verbally says adjusting but nothing happens** — prompt optimization in progress
3. **Physical restart required after flashing** — Unplug USB, wait 30s, then plug back in
4. **Wake word sensitivity may need adjustment** — Use High for uncommon words, Medium for common ones
5. **Self-hosted server security**: `auth.enabled=true`, MAC whitelist, never expose API keys, don't expose ports unprotected

</details>

---

## 📋 Changelog

<details>
<summary>Click to expand</summary>

- **2026-06-16** · **v0.0.2** released
- **2026-06-16** · Config page enhancements: 🤖 robot icon + "StackChan" branding + theme toggle (auto/light/dark) + color-coded signal bars + CN/EN languages only; New `self.face.expression` MCP tool (19 emotions) + `self.get_system_info` promoted to public tool; WiFi connect timeout 60s → 10s + auto-clear bad credentials after 3 failures + clear on long-press; Local preview: `node scripts/wifi-preview.js`
- **2026-06-16** · esp-wifi-connect v3.1.5 → v3.2.1: CSS design tokens + dark mode + gradient background + scan animation + 45 languages; OTA config and sleep mode visibility enabled
- **2026-06-15** · Wi-Fi config page (192.168.4.1) redesign: iOS-style segmented control + large rounded cards + signal strength visualization + StackChan branding
- **2026-06-11** · Fixed sleep mechanism (restored idle check, no more false sleep during conversation, timeout 60s → 30s); Idle long-press screen 5s to enter Wi-Fi config mode; Mute BMI270 motion sensor during speech to prevent servo shake from triggering IMU
- **2026-06-10** · Build fixes + version check retry optimization (`MAX_RETRY=1`) + default WebSocket protocol; Touch/SendUserText long text interception fix (≤24 bytes); Disabled remote OTA upgrade; Split motion pools into `display+tag`, removed morning greeting, removed `upgrade_firmware` MCP tool
- **2026-06-06** · Wake word sensitivity Low/Medium/High three levels; Text interruption optimization; Sleep backlight fully off; SPIRAM changed back to QUAD

</details>

---

## 🙏 Acknowledgments

- [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) — Base firmware
- [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) — Server
- [M5Stack StackChan-BSP](https://github.com/m5stack/StackChan-BSP) — SI12T reference
- [esp-wifi-connect](https://github.com/78/esp-wifi-connect) — WiFi configuration component

## 📄 License

Same as upstream [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32).
