# CHANGELOG v08.12（2026-08-12）

## Codex 回复必播兜底（新增 scripts/session_watcher.py + 网关集成）

- **背景**：Codex 桌面应用重启后，续传会话（如 019fd205）不再触发任何 hooks
  （新会话正常），导致 Codex 助手回复不进播报队列、机器人静默；此前
  `push_direct_done=true` 只解除"是否播报"开关，前提是 Stop 钩子先触发，续传会话不触发
  则开关无从生效。
- **方案**：新增 `scripts/session_watcher.py` 兜底监听器，直接扫描
  `~/.codex/sessions/**/rollout-*.jsonl`，按 `turn_id` 提取每轮最终助手文本；轮次完成后
  （文件 25s 无写入判静默完成）经网关 `/api/agent_event` 上报
  `agent=codex, event=done, msg_uid=watcher-<turn_id>`，机器人自动播报，与钩子互斥。
- **防双播判据**：会话 10 分钟内 codex_hook.state.json 有 done 记录 **且** 该会话最近一条
  Stop 日志带 `transcript_path`（真实钩子事件）才跳过；手工/模拟报文（无 transcript_path）
  不判健康，避免失效续传会话被静默压制。
- **集成**：网关 `_session_watcher_loop` 线程随网关启动，每 5s 扫描一次；
  watcher 状态写入 `state/session_watcher.state.json`（last_turns 持久化当前轮次，
  防轮次结束后无新数据漏检）。

## 确认/权限请求播报补全（agents/claude_hook.py）

- PermissionRequest 摘要提取补 `question / plan / message` 字段；
- AskUserQuestion / ExitPlanMode 只播具体问题/计划内容，不再只念英文工具名；
- 真机验证：`claude 需要确认: …` push ack；触屏/语音确认回环
  （confirm → confirm_answer_by_uid → 结果播报）通过。

## 句尾吞字补丁（gateway/fusion_gateway.py）

- EdgeTTS 帧流末尾追加 4 帧 µ-law 静音（240ms），确保末词在硬件 DMA 中完整发声后再发
  STOP，根治句尾吞字；
- 本地兜底 sherpa-onnx `tts_fallback_speed` 默认 1.0（EdgeTTS rate +0% 已在 v08.10 归一）。

## 保固（scripts/hook_health.py）

- 新增 `check_watcher()`：session_watcher 脚本语法 + 状态文件心跳检查
  （120s 无更新判线程已停），纳入 5 分钟周期自检，异常时机器人告警。

## 真机验证（2026-08-12 19:49）

- watcher 兜底：Codex 续传会话回复写完 25s 后自动播报，push ack；
- question：`claude 需要确认: 系统自检…` push ack + confirm 回环 allow；
- hook 自检：全部正常（exit 0）。
