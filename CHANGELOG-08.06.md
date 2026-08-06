# StackChan 融合方案 · 成果存档 v08.06（2026-08-06 收尾）

> 日期: 2026-08-06 · 云链路 + EMQX MQTT 主动播报闭环日。

## 今日成果总览

### 1. 播报链路根治（"没有声音 / 1秒杂音 / 卡顿"）

| 问题 | 根因 | 修复 | 验证 |
|---|---|---|---|
| 播报完全无声 | 网关 opus.dll 帧头非标准，ESP 预编译解码器每 60ms 帧只解 1/6 | 弃 Opus，改传 **µ-law(G.711)** 16KB/s，固件查表还原 s16le | 9.5s 粤语完整播报，用户确认"播报成功" |
| 1 秒杂音后静音 | MQTT 报文超 ESP 缓冲被截断 → 读超时断连掐播 | MQTT buffer 2048→8KB；poll 读超时 1s→5s | 无截断/无超时 |
| 卡顿/时断时续 | 公网 EMQX RTT ~0.5s × lwIP 收窗口 5760B → 吞吐只有 ~11KB/s | lwIP TCP 窗口→16KB（µ-law 下 ~31KB/s 有余量）；8帧/批+50ms 节流 | 全程无卡顿 |
| 批量报文被信标拖慢 20s+ | WiFi MAX_MODEM 节能 | 播报期间 `WIFI_PS_NONE`，播完恢复 | 突发 1~2s 送达 |
| 播报完卡蓝灯 | STOP 立即切 idle 与播放队列不同步 | STOP 只标记流结束，等队列排空由看门狗切待机 | 播完自然回暖橙 |

### 2. 主动播报闭环（msg_uid 幂等 + 固件 ACK 点杀）

- 网关：**单 Worker FIFO** 串行推流（`queue.Queue`），所有推送入口统一入队；
  语音 150 字口语摘要（完整原文保留 pending + 日志）。
- **msg_uid 全链路幂等**：三个 Hook（codex/claude/antigravity）按
  `(session, 最后一条 assistant 消息)` 生成唯一 uid，废除 120s 滑动窗口去重；
  `/api/agent_event` 按 uid 幂等（重复上报静默 200）；pending 主键=msg_uid；
  START 报头 `\x01+uid+\x00+text`；固件回发 ACK → 网关 **ACK-and-Delete 物理删除**。
- 离线兜底：无 ACK → 保留 pending + 30s 退避自动重试 + 5min TTL；唤醒后
  `agent_pending` 补播。

### 3. Agent 耦合补齐

- 别名归一化：`可头大/扣代码/扣德斯→codex`、`反重力/安特格拉维蒂→agy`、`克劳德→claude`；
  `agent_query/status/confirm` 全部接入。
- Fail-Fast 存活预检：agent 未安装/不可用立即返回"未在电脑启动"，不再死等 120s。
- **VS Code 接入**：`AGENT_CLIS` 注册 vscode + `agents/vscode_hook.py` 完成上报。
- **Claude 交互式确认**：`~/.claude/settings.json` 增加 PermissionRequest 钩子，
  权限弹窗主动播报；Stop/SessionEnd/Notification/PermissionRequest 四钩子命令改正斜杠
  （bash 兼容，修复 `command not found`）。
- **Codex↔机器人桥接恢复**：`bridge/stackchan_mcp.js`（MCP stdio），respond 直接入网关
  pending；`~/.codex/config.toml` 启用。

### 4. 配置防呆与可观测性

- config.json 非法 → **Fail-Fast 拒启**（`[FATAL]` 日志），不再静默回退默认配置。
- 5 个 hook 脚本（codex/claude/antigravity/confirm_mcp/vscode）配置解析失败显式 ERROR。
- Hook 上报成功/失败全量日志；`claude_hook` 补上缺失的 `_log`（此前每次上报后崩溃）。
- Codex SessionEnd hook 超时 10s→3s 对齐钳制；`codex_hook` 内部 POST 超时 2.5s。

### 5. 托盘与工具

- 托盘按云链路口径重写：待推送(pending)/待播报事件/待确认分开展示；机器人链路显示
  "最近推送"（云链路健康代理）；自建服务器标注已停用。
- `robot_status` 重写为云链路自检（云桥接心跳/网关工具/推送链路/机器人活动）。
- 清理死配置：`push_api_url`/`push_secret`（旧 8003 /api/push 链路）移除。
- 队列清理：progress 事件 + 过期待确认可一键清空（托盘），ACK 后 pending 物理删除。

## 固件版本

- **v1.2-mqttpush**（已刷入）：µ-law 播放、第二条 MQTT 推送链路、msg_uid 解析 + ACK 回执、
  keepalive 15s、MQTT buffer 8KB、poll 超时 5s、lwIP 窗口 16KB、播报期关 WiFi 节能、
  STOP 后自然收尾、SSID 智能路由（EMQX 首选 / LAN 次选 / Tailscale / Funnel）。
- 构建：`firmware/build_fw_v112.ps1`（espressif/idf:v5.5.2）；app-only 刷 `0x410000` 保留配置。

## 部署要求（新机器）

见 `DEPLOY.md` + `gateway/config.json.example`（全部占位符）。

## 回退

- 固件回退：`firmware/post-fw-v1.0.6-ttsbuf/`（上一版云链路固件）。
- 播报链路回退点：v08.06 本次提交即回退基线。
