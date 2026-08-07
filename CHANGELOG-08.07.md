# StackChan 融合方案 · 成果存档 v08.07（2026-08-07 收尾）

> 日期: 2026-08-07 · Phase 8 动作联动 + CoreS3 视觉 MCP + Claude hooks 抗覆盖加固日。

## 今日成果总览

### 1. Phase 8.1 舵机与表情动作深度联动 (Motion Engine)

- 固件 `PushMqttEvent`：`etype == "done"/"error"` 广播 → 舵机微点头 `Nod`；
  `etype == "question"` → 头偏转 +15° `TiltAsk`；已真机确认。
- 待机闲逛摆头 4s → **20s**（`kIdleScanIntervalUs = 20000000` 统一 Begin/ResumeScan，
  消灭遗留的 4000000 硬编码），真机实测 20s 间隔。

### 2. Phase 8.2 CoreS3 视觉 MCP 模块（robot_snap）

- 网关新增 `robot_snap` MCP 工具（工具总数 11 → **12**）：机器人拍一张桌面照片 →
  JPEG 分块 MQTT（`stackchan/{mac}/photo`，QoS1）→ 网关重组并校验 JPEG 魔数 +
  总长度；已真机连拍 3/3 有效。
- 效果：Codex / Claude / Antigravity 可以直接"看"桌面物理实体与屏幕，
  网关工具列表含 `robot_snap`（`/healthz` 可见）。

### 3. Claude Code hooks 抗覆盖加固（五工单审计 2026-08-07）

| 工单 | 内容 | 验收 |
|---|---|---|
| 1 | `install_claude_hooks.ps1` 目标改为 `~/.claude/settings.local.json`（ccswitch 只覆盖 settings.json 的 env 段，local 优先级更高 → hooks 不再被反复抹掉） | `True ['SessionEnd','Notification','Stop','PermissionRequest']` |
| 2 | 脚本末尾 ccswitch 自愈保障提示（丢 hooks 时重跑脚本即恢复） | ✅ |
| 3 | `agents_core.query()` 对 `vscode` 显式拒发语音派发（"VS Code 暂不支持语音派发任务…"），杜绝 `code -r <task>` 把任务文本当文件打开 | 实测 `query('vscode')`/`query('code')` 均拦截 |
| 4 | `claude_hook.py` 空摘要兜底：Stop/SessionEnd 摘要为空时强制上报 "Claude 会话结束(响应可能中断, 详见电脑)"，流式中断不静默丢事件 | 实测已推流 |
| 5 | `~/.codex/hooks.json` 清理 15 条 PromLight 僵尸钩子（备份 `hooks.json.bak-20260807-110621`），仅保留 codex_hook 5 大事件（SessionStart/UserPromptSubmit/PermissionRequest/Stop/SessionEnd，均带 timeout） | ✅ |

### 4. 其它

- VS Code 半自动闭环：`vscode_hook.py --install-tasks` 生成 `.vscode/tasks.json`；
  Agents 手动调用 `vscode_hook.py --summary "..."` 亦可上报 done。
- 网关 `_photo_state`/`_photo_lock` 照片重组状态机，photo 主题监听与 ACK 同链路。

### 5. Antigravity 桌面播报修复（13:29，根治"重启后依然不播"）

- 死穴一：`~/.gemini/config/hooks.json` 顶层命名空间被误改为 `"fusion"`，Antigravity IDE
  只识别规范命名空间 `"stackchan"` / `"promlight"`，整段 Hook 被静默跳过 → 已还原为
  `"stackchan"`（PreToolUse/PostToolUse/PermissionRequest/PermissionDenied/Elicitation/Stop）。
- 死穴二：`agents/antigravity_hook.py` 事件名只匹配 `"Stop"`，而 IDE 实际上报
  `"AfterAgent"`/`"agent.stop"` → 已升级为
  `elif event in ("Stop", "AfterAgent", "agent.stop", "SessionEnd", "agent.session.end")`。
- 验收：AfterAgent 模拟报文实测 `posted done ok antigravity_test-aft_*`；13:29:16 网关日志
  `push ack [agent]: agy 任务完成`（真机播报 + ACK），pending 清空。
- 约束：hooks.json 顶层命名空间**严禁再改成 "fusion"**。

### 6. LED 灯环根治 + Prompt v3.6 保固（15:0x，真机确认播报绿→待机暖橙）

- 根因：**PY32 GPIO13 未配置输出推挽**（对照 M5Stack 出厂固件 hal_io_expander，
  WS2812×12 灯环驱动前必须设 0x04/0x0A/0x0C/0x14 bit5）→ 0x24/0x30 写入被
  PY32 接受但灯环不驱动，灯恒为初始橙色。另有 mqtt push 重构丢失的
  Phase 7.1 三防护（写失败静默、`led_manual_` 永久屏蔽状态色、I2C 无互斥）。
- 固件（`reference/stackchan-xiaozhi-firmware-mqtt/.../m5stack_core_s3.cc`）：
  `InitializePy32LedDevice` 补齐 GPIO13 输出+上拉+推挽出厂序列；`Py32WriteRegBlock`
  失败日志+重试；`refreshLeds` 读-改-写；`led_manual_` 仅待机生效、活跃态强制
  状态色、离开自动复位暖橙、Idle 待机锁；恢复 `i2c_bus_mutex_`（FreeRTOS
  Mutex+50ms）+ 触屏 I2C 故障冷却 + 400ms 防踩踏。
- Prompt：阿松 v3.5 → **v3.6**（LED 灯色归固件，严禁 LLM 调灯色表达情绪；
  用户明确要求时同轮 `self.led.auto` 恢复）——需贴回 xiaozhi.me 控制台。
- 刷机：app-only @0x410000，串口实测 `STATE LED -> speaking/neutral` 正常、
  无 `PY32 I2C write failed`，网关 push ack 闭环；**真机目视：播报变绿、
  播完回暖橙 ✅**。聆听蓝待「阿松」唤醒验证。

## 固件版本

- **v1.2-mqttpush**（继续使用）：µ-law 播放、msg_uid/ACK 闭环、Phase 8.1 动作联动
  （done→Nod / question→TiltAsk）、待机摆头 20s、拍照分块上报（type=4）。
- 构建：`firmware/build_fw_v112.ps1`（espressif/idf:v5.5.2）；app-only 刷
  `xiaozhi.bin @ 0x410000` 保留配置。

## 验收

- 五工单验收证据全部回传（见上表）；`~/.claude/settings.local.json` 实测含四钩子；
- `robot_snap` 连拍 3/3 有效；动作联动与 20s 摆头真机确认；
- 网关 PID 运行中，`/healthz` 显示 12 工具，pending 为空。

## 待办 / 提醒

- ~~重启 Antigravity 桌面版让修复生效~~ → 已重启，13:29 真机 ACK 验证通过。
- VS Code 自动上报为半手动：在项目里跑 `vscode_hook.py --install-tasks` 生成 tasks.json。
- GitHub 同步：v08.07 起 `<TAILSCALE_IP>`（Tailscale IP）与真实本地路径已从公开副本移除。
