# StackChan 融合方案 · 成果存档 v08.03

> 日期: 2026-08-03 · 本文件夹是当日成果快照, 完整迁移包见 `package-stackchan-08.03.zip`。

## 今日成果（2026-08-03）

### 1. 链路修复（全部实测）

| 问题 | 修复 | 验证 |
|---|---|---|
| codex 任务 Access denied / 看不到进程 | `~/.codex/config.toml` `[windows] sandbox: elevated→unelevated`; 去掉 `--sandbox workspace-write` | 可见窗口跑 shell 任务 0 报错 |
| 机器人「检查 agent 状态」超时 | agents_core 探测缓存 120s + 4 agent 并发 | 13.9s → 4.8s（缓存后 <1s） |
| 云链路中文任务乱码 | hook 脚本改读 stdin.buffer + UTF-8; mcp_pipe 子进程 PYTHONUTF8=1 | 中文事件干净 |
| 机器人播报陈旧结果（"colback"=8/1 旧查询） | `agent_result_check` 只返回 30 分钟内新结果, 过期归档; outbox 清空 | 不再念旧内容 |
| system tray 双图标 | fusion_tray.ps1 单实例保护 | 仅 1 个托盘 |
| 计划任务弹 PowerShell 窗口 | 全部改 wscript.exe + VBS 隐藏启动 | 无闪窗 |
| mcp-endpoint health key 失效 | config.json 改为容器实际 key | 备用链路 health 200 |

### 2. 固件 v1.0.2-micfix（语音识别修复）

- 基座: 07.31 已跑通的 `reference/stackchan-xiaozhi-firmware`（**不是** HtSz 主分支, 后者有 bug 起不来）
- 改动: 仅麦克风输入增益 30→42; 唤醒词「阿松」(a song); 阈值下限 0.35 保持
- 二进制尺寸与跑通版完全一致（xiaozhi.bin 2,961,376 / merged 15,556,642）
- 构建: espressif/idf:v5.5.2; 已刷入机器人（app-only @ 0x410000）

### 3. 云链路 + 唤醒播报（Phase 1-3 完成）

- **Prompt v2**: 唤醒优先规则（每次唤醒先 `agent_pending`, 逐条播报, 念完 clear=true）;
  「查询 XX 状态」→ agent_status（绝不 agent_query）; 见 `prompt-阿松-v2.md`
- 四 agent 接入: codex（hooks）/ claude（hooks + 确认回环）/ agy（fusion hooks）/ pi（扩展）
- 可见窗口执行: 机器人任务在 agent 自己的窗口跑, 结果经 hooks 回流
- 事件口语化: 去 markdown/路径, 适合语音朗读
- 验证: 唤醒播报 ✅ / 状态查询 ✅ / 任务闭环待最终验收

### 4. 工程资产

- `MEMORY.md`: 权威项目记忆（架构/服务/固件/修复清单/待办）
- `package-stackchan.zip`: 全量迁移包（固件 + PC 端 + README + prompt + MEMORY）
- GitHub 发布副本: `stackchan-fusion-github/`（已脱敏, 供他人部署）

## 文件清单

| 文件 | 说明 |
|---|---|
| package-stackchan-08.03.zip | 全量迁移包（含固件 v1.0.2-micfix / PC 端全套 / 文档） |
| MEMORY.md | 项目记忆（会话接续用） |
| prompt-阿松-v2.md | 云智能体提示词（已贴入 xiaozhi.me 控制台） |

## 部署到新机器人/新电脑

见 GitHub 副本的 `DEPLOY.md`（多机器人部署指南, 已脱敏）。
