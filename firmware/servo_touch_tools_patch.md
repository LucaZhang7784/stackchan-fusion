# 机器人能力补丁：舵机动作 MCP 工具 (self.servo.*) — 基于 hylarucoder/StackChan 参考

适用固件: <PROJECT_ROOT>\merge-v226 (xiaozhi-esp32 2.2.6 + m5stack-core-s3 板)

## 现状 (merge-v226 板代码 m5stack_core_s3.cc 已具备)

| 能力 | 实现位置 | MCP 工具 |
|---|---|---|
| 摄像头 | InitializeCamera() + EspVideo | self.camera.take_photo (mcp_server.cc 标准工具) |
| LED 12颗 | RegisterLedMcpTools() + PY32 | self.led.set_color / turn_off / auto |
| 摸头 | SI12T 三区触摸 + FT6336 摸头手势 -> SendUserMessage("（主人摸了摸小智的头）") | 无工具(事件注入对话, LLM 会回应) |
| 舵机 | StackChanServo (SCSCL UART1, MoveTo/Nod/Shake/Tilt/Center + 空闲扫视) | **无工具, 需新增** |

结论: 相机/LED/摸头开箱即用(需刷 merge-v226); 只有"Agent 主动命令动作"缺 self.servo.* 工具。

## 补丁: 在 m5stack_core_s3.cc 添加 RegisterServoMcpTools

在 RegisterLedMcpTools() 函数后面追加:

```cpp
    void RegisterServoMcpTools() {
        auto& mcp = McpServer::GetInstance();
        mcp.AddTool("self.servo.move",
            "Move the StackChan head. yaw: -45..45 degrees, pitch: 5..60 degrees, time_ms: 100..2000.",
            PropertyList({
                Property("yaw", kPropertyTypeInteger, -45, 45),
                Property("pitch", kPropertyTypeInteger, 5, 60),
                Property("time_ms", kPropertyTypeInteger, 100, 2000),
            }),
            [this](const PropertyList& props) -> ReturnValue {
                servo_.MoveTo(props["yaw"].value<int>(), props["pitch"].value<int>(), props["time_ms"].value<int>());
                return true;
            });
        mcp.AddTool("self.servo.nod",
            "Nod the head (yes).", PropertyList(),
            [this](const PropertyList&) -> ReturnValue { servo_.Nod(); return true; });
        mcp.AddTool("self.servo.shake",
            "Shake the head (no).", PropertyList(),
            [this](const PropertyList&) -> ReturnValue { servo_.Shake(); return true; });
        mcp.AddTool("self.servo.home",
            "Return the head to center.", PropertyList(),
            [this](const PropertyList&) -> ReturnValue { servo_.Center(); return true; });
    }
```

在构造函数里 RegisterLedMcpTools(); 之后追加一行:

```cpp
            RegisterLedMcpTools();
            RegisterServoMcpTools();
```

可选: 想给 Agent 查询最近触摸事件, 再注册 self.touch.last_event
(SI12tLoop 里把最近 msg 存入成员变量, 工具里返回)。

## 编译烧录 (OUTPUT.md 命令)

cd <PROJECT_ROOT>\merge-v226
docker run --rm -v "//d/<PROJECT_ROOT>/merge-v226:/project" -w //project ^
  espressif/idf:v5.5.2 bash -c "idf.py set-target esp32s3 && idf.py build"
python -m esptool --chip esp32s3 --port COM8 erase-flash
python -m esptool --chip esp32s3 -b 460800 --port COM8 --before default-reset --after hard-reset ^
  write-flash --flash-mode dio --flash-size 16MB --flash-freq 40m ^
  0x0 build/bootloader/bootloader.bin ^
  0x8000 build/partition_table/partition-table.bin ^
  0xd000 build/ota_data_initial.bin ^
  0x20000 build/xiaozhi.bin ^
  0x800000 build/generated_assets.bin

烧录后拔 USB + 电池 30 秒再上电。

## 服务器/控制台: 零改动

- 设备工具由固件 hello 时自动上报 (DEVICE_MCP), 上线后自动出现在服务器函数列表
  与智控台设备调用模块, 无需配置。
- (可选) 想让 LLM 更主动用工具: 编辑 server\data\.agent-base-prompt.txt 提示词,
  参考 fusion.firmware.0731\server\prompt_patch.md。

## 参考 repo 说明 (hylarucoder/StackChan)

- 它是 M5Stack StackChan 出厂固件 + xiaozhi-esp32 v2.2.4 (patches/xiaozhi-esp32.patch
  + hal_bridge.cc 桥接 HAL/触摸/电池到 xiaozhi Board)。
- 出厂固件完整度更高(表情动画/跳舞/遥控 App), 但 merge-v226 已用更轻量的方式
  (直接改 m5stack-core-s3 板代码) 实现了相同的硬件能力, 无需整体换固件。