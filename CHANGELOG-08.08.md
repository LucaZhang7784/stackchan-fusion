# StackChan 融合方案 · 成果存档 v08.08（2026-08-07 午后）

> 日期: 2026-08-07 · 下午修复批次（Antigravity 播报根治 + LED 灯环根治 + Prompt v3.6 + 固件重建）。

## 1. Antigravity 桌面播报修复（13:29 真机 ACK）

- 死穴一：`~/.gemini/config/hooks.json` 顶层命名空间被误改为 `"fusion"`，Antigravity IDE 只识别
  规范命名空间 `"stackchan"` / `"promlight"`，整段 Hook 被静默跳过 → 已还原为 `"stackchan"`。
- 死穴二：`agents/antigravity_hook.py` 事件名只匹配 `"Stop"`，而 IDE 实际上报
  `"AfterAgent"` / `"agent.stop"` → 已升级为
  `elif event in ("Stop", "AfterAgent", "agent.stop", "SessionEnd", "agent.session.end")`。
- 验收：AfterAgent 模拟报文实测 `posted done ok`；13:29:16 网关日志 `push ack [agent]: agy 任务完成`
  （真机播报 + ACK），pending 清空。
- 约束：hooks.json 顶层命名空间**严禁再改成 "fusion"**。

## 2. LED 灯环根治（真机确认播报绿 → 待机暖橙）

- 根因：**PY32 GPIO13 未初始化**。对照 M5Stack 出厂固件（hylarucoder-StackChan hal_io_expander）：
  灯环 WS2812×12 驱动前必须把 GPIO13 配为 **输出+上拉+推挽**（REG_GPIO_M_H=0x04 / PU_H=0x0A /
  PD_H=0x0C / DRV_H=0x14 的 bit5）。此前缺这步，0x24/0x30 写入被 PY32 接受但灯环不驱动，
  "写成功但灯不变"。
- 固件改动（`reference/stackchan-xiaozhi-firmware-mqtt/.../m5stack_core_s3.cc`）：
  - `InitializePy32LedDevice` 补齐 GPIO13 出厂序列；新增 `Py32ReadReg()` / `Py32SetRegBit()`；
  - `Py32WriteRegBlock` 失败日志 + 一次重试；`refreshLeds` 改**读-改-写**（保留 CFG 其他位）；
  - `led_manual_` 仅待机生效 / 活跃态强制状态色 / 离开自动复位暖橙 / Idle 待机锁；
  - 恢复 `i2c_bus_mutex_`（FreeRTOS Mutex + 50ms 超时）+ 触屏 I2C 故障冷却 + 400ms 防踩踏。
- 固件重建：`build_fw_v112.ps1`（espressif/idf:v5.5.2）→ app-only 刷 `xiaozhi.bin @ 0x410000`
  （保留配置）；14:31 产物已更新（xiaozhi.bin 2968656B，`firmware/post-fw-v1.2-mqttpush/`）。
- 真机日志：`STATE LED -> speaking` → `neutral`，无 `PY32 I2C write failed`，网关 push ack 闭环；
  **目视：播报变绿、播完回暖橙 ✅**。聆听蓝待「阿松」唤醒验证。

## 3. Prompt v3.6（LED 保固）

- 阿松 v3.5 → **v3.6**：LED 灯色归固件，**严禁 LLM 用 `self.led.*` 表达情绪**；用户明确要求
  调灯时必须同轮 `self.led.auto` 恢复。
- 文件：`prompt-阿松-v3.md`（需贴回 xiaozhi.me 控制台）。

## 4. Antigravity hooks 结构根治（15:46, 真机复现→铁证→修复）

- 现象：Antigravity 桌面版 15:22 崩溃重启后，hook 事件全部不再到达 `antigravity_hook.py`，
  结论不进队列、机器人不播报（`antigravity_hook.log` 最后写入 13:29，会话 transcript 活动到 15:23）。
- 铁证：`%APPDATA%\Antigravity\logs\language_server.log` 15:23:27：
  `hooks.go:44 Failed to parse hooks file ...: invalid hook "stackchan": command hook
  must specify 'command'` —— 11:16 改成的**顶层命名空间结构**（stackchan/promlight）被
  语言服务器**整文件拒绝**。
- 真相：Antigravity 桌面版只认**标准 Gemini hooks 结构**（顶层 `hooks` 键 +
  `matcher` + `hooks[].command`）；支持事件名（从 language_server.exe 确认）：
  SessionStart / PreToolUse / PostToolUse / PermissionRequest / PermissionDenied /
  Elicitation / Stop / SessionEnd / Notification。
- 修复：`~/.gemini/config/hooks.json` 重写为标准结构（7 事件，matcher `.*`，无 timeout；
  promlight 段移除，备份 `hooks.json.bak-20260807-hooksgo`），JSON 校验通过、无 BOM。
- 防复发：改 hooks.json 后必须重启 Antigravity 桌面版并确认 language_server.log
  无 `Failed to parse hooks file`；MEMORY.md #16 已记录。

## 5. 待办 / 下一阶段

- 唤醒「阿松」聆听蓝灯验证（日志从未出现 `STATE LED -> listening`，疑云链路不驱动本地聆听态，需单独查）；
- v3.6「调红」保固测试；
- **Phase 9 计划待批**：9-D 网关（TTS SHA256 缓存 + 本地粤语兜底 + `local_query` Ollama 工具）
  → 9-B 触屏确认闭环（LVGL 批准/拒绝浮层 + `stackchan/{mac}/confirm`）→ 9-A 行为状态机；
- 本地粤语 TTS 调研：Ollama 无 TTS 能力；推荐 sherpa-onnx `vits-cantonese-hf-xiaomaiiwn`（小美）
  作断网兜底（EdgeTTS 正常时仍走云端）。

## 6. 归档

- 本地 `version.08.08/` 全量快照（含 LED 修复固件产物 + firmware-src 源码快照 + restore.md +
  SUMMARY-2026-08-08.md）；
- GitHub 公开副本同步（脱敏：MAC / Tailscale IP / 域名 / 密钥 / 用户路径 → 占位符）。

