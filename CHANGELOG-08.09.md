# StackChan 融合方案 · 成果存档 v08.09（2026-08-07 晚）

> 日期: 2026-08-07 · 晚批次（Claude hooks Windows 失效根治 + 托盘自愈菜单 + 网关两轨吞字修复）。

## 1. Claude hooks Windows 静默失效根治（settings.local.json → settings.json）

- 现象：Claude session `56b9df49` 任务完成后未进播报队列、未播报；最小复现
  `claude -p` 也完全不触发 hooks（claude_hook.log 行数不变）。
- 根因：Claude Code 2.1.224（17:35 升级）在 Windows 上对 `settings.local.json` 的 hooks
  静默失效，命中已知 BUG [anthropics/claude-code#64699](https://github.com/anthropics/claude-code/issues/64699)
  （编辑 settings.local.json 后所有 hooks 停止触发，重启/回滚均无法恢复）；手工喂 stdin
  给 claude_hook.py 能正常上报 → 确认问题在 Claude Code 侧不执行 hook，而非脚本/网关。
- 修复：4 个 hooks（Stop/SessionEnd/Notification/PermissionRequest）主存迁移到
  `~/.claude/settings.json`（用户级全局，规避该 Windows BUG）；真机验证
  `claude -p` 触发 Stop+SessionEnd、`posted done ok`、机器人 ACK 播报（18:00:59）。
- 补播：56b9df49 完成消息（"两份文件均已生成…"）经标准 hook 链路补报 → 17:59:11
  机器人 ACK 播报，pending 清零。

## 2. install_claude_hooks.ps1 重写（PowerShell 5.1/7 双兼容）

- 编码：**UTF-8 BOM**（5.1 读无 BOM 文件按 GBK 误读中文 → "The string is missing the terminator"）。
- 兼容：删除 `ConvertFrom-Json -AsHashtable`（**PS7 专属**，5.1 抛错被 catch 吞掉会
  **清空 settings.json 的 env 段**）→ 改 PSCustomObject 幂等合并，保留 env/permissions。
- 目标：hooks 写入 `settings.json`；ccswitch 切模型覆盖后重跑即自愈（脚本幂等）。

## 3. 托盘新增「安装/修复 Claude Hooks」菜单

- `gateway/fusion_tray.ps1`：菜单项位于「查看状态详情」与「重启网关」之间；点击同步运行
  install_claude_hooks.ps1 并弹窗显示结果（成功/失败 + 重启会话提示）。
- 语法经 pwsh 与 Windows PowerShell 5.1 双解析验证；托盘已重启生效（PID 61636→34640 网关侧）。

## 4. 网关两轨修复（长文本吞字根治）

- **轨一 · TCP 分片丢包**：`_PUSH_BATCH_FRAMES = 8 → 2`（每批 2 帧 = 120ms ≈ 1.9KB
  < MTU），不再触发 TCP 分片，根治固件因 `offset != 0` 丢帧吞字。
- **轨二 · LLM 60 字口语化摘要**：新增 `_summarize_for_speech(text, max_chars=60)`——
  >60 字经本地 Ollama 提炼 ≤60 字口语摘要；LLM 不可用/超时/输出异常降级 `text[:60]`；
  送入 TTS 前完成摘要（START 字幕与音频一致）；新增 `_prewarm_local_llm` 启动预热。
- push 日志改记**实际播报文本**（sent_text），不再显示误导性原文。
- 验收：POST 221 字长文本 → 18:23:54 机器人 ACK 播报 50 字 LLM 摘要
  （"武汉众宇动力资产收购项目方案已验收尽调方法论重大升级…"）；pending 清零。

## 5. 配置修正

- `gateway/config.json`：`local_llm_model` qwen3:8b（**已被删除**）→ **qwen3.5:9b**（实测可用），
  `local_query` 与摘要 LLM 一并复活；启动预热日志 `本地 LLM 预热完成: qwen3.5:9b`。

## 6. 待办 / 下一阶段

- 长文本播报 100% 完整吐字需真机听感最终确认（摘要已缩短至 ≤60 字）；
- 唤醒「阿松」聆听蓝灯验证（历史待办）；v3.6「调红」保固测试；
- Phase 9-B 触屏确认闭环 / 9-A 行为状态机（固件侧，下一轮）。

## 7. 归档

- 本地 `version.08.09/` 全量快照（含 README/CHANGELOG/MEMORY/restore/SUMMARY + 网关/脚本/托盘最新版）；
- GitHub 公开副本脱敏同步（MAC / Tailscale / 域名 / 密钥 / 用户路径 → 占位符）。