# StackChan 融合项目记忆（权威版）

> 更新: 2026-08-03 18:00 (+08:00) · 会话开始时先读本文件再动手。

## 一、当前架构（云链路 + 唤醒播报）

```
机器人(M5Stack CoreS3, 固件 v1.0.2-micfix)
  │ 语音走 xiaozhi.me 云端 STACK 智能体(ASR/LLM/TTS 全在云端)
  ▼
xiaozhi.me 云 LLM ──MCP──► xiaozhi-mcp 桥接(mcp_pipe.py + server.py, 本机)
                              │ agent_status / agent_query / agent_pending /
                              │ agent_confirm / agent_result_check / docker_status ...
                              ▼
                        融合网关 fusion_gateway.py (:8010, 11 工具)
                              │
        ┌─────────────────────┼──────────────────────┐
   codex(CLI/桌面, hooks)  claude(hooks+确认回环)  agy/Antigravity(fusion hooks)  pi(扩展)
   （机器人任务在 agent 自己的可见窗口执行, 结果经 hooks 回流网关→机器人唤醒播报）
```

- **主链路**: 云链路; 自建 docker xiaozhi-esp32-server + Tailscale Funnel 只作备用。
- **主动播报方式**: 唤醒优先规则——每次唤醒后 LLM 先调 `agent_pending`, 逐条念, 念完 `clear=true`（非打断式; 自建链路才有 robot_say 真推送）。
- **唤醒词「阿松」**（拼音 a song, 固件硬编码 + 你好小智兜底; 阈值代码下限 0.35 防误触发）。

## 二、服务状态（2026-08-03 实测）

| 服务 | 端口 | 状态 |
|---|---|---|
| 融合网关 fusion_gateway.py | 8010 | ✅ /healthz ok, 11 工具 |
| xiaozhi-mcp 云桥接 (mcp_pipe + server.py) | — | ✅ 心跳 60s 正常; 开机自启任务 StackChan-CloudBridge(wscript 隐藏) |
| xiaozhi-esp32-server (Docker, 备用) | 8000/8003 | ✅ Up healthy |
| mcp-endpoint-server (Docker) | 8004 | ✅ Up healthy |
| xiaozhi web / redis / db | 8002/6379/3306 | ✅ Up |
| funnel_proxy.py (备用路由) | 8090 | ✅ 运行中 |
| Tailscale Funnel | 443 | ✅ https://dahuilucaaaaa.tail61f3fa.ts.net |
| 系统托盘 fusion_tray.ps1 | — | ✅ 单实例（有保护） |

## 三、机器人固件（重要）

- **当前固件**: `fusion.firmware.0731/firmware/post-fw-v1.0.2-micfix`
  - 基座: **07.31 已跑通的** `reference/stackchan-xiaozhi-firmware`（heavenchenggong 系, 含「阿松」+ LED 补丁, 不要用 HtSz 主分支——有 bug 起不来）
  - 改动: 仅麦克风输入增益 30→42（修复识别差）; 唤醒词 阿松; 阈值代码下限 0.35
  - 布局: post-fw（app @ 0x410000, 16MB）; app-only 刷 `xiaozhi.bin @ 0x410000` 保留配置
  - 构建: espressif/idf:v5.5.2（5.5.4 会黑屏）, `firmware/build_led_fw.ps1` / `build_led_ci.sh` 流程
- 麦克风增益文件: `reference/stackchan-xiaozhi-firmware/main/boards/m5stack-core-s3/cores3_audio_codec.cc` (`input_gain_`)
- 唤醒词: 固件 `custom_wake_word.cc` 硬编码 `{"a song","阿松","wake"}` + 你好小智兜底

## 四、四 agent 接入要点

| Agent | 方式 | 状态 |
|---|---|---|
| codex | `~/.codex/hooks.json`(11 事件→codex_hook.py) + `config.toml` `bypass_hook_trust=true` + `[windows] sandbox='unelevated'`（elevated 会间歇 Access denied）+ 不用 `--sandbox workspace-write` | ✅ 桌面+CLI 都上报 |
| claude | `~/.claude/settings.json` hooks→claude_hook.py + confirm_mcp.py（确认回环完整: 语音回答可回写 allow/deny） | ✅ |
| agy/Antigravity | `~/.gemini/config/hooks.json` fusion 段; CLI 按 artifactDirectoryPath 含 antigravity-cli 归属 agent=agy | ✅ |
| pi | `~/.pi/agent/extensions/hooks-bridge.ts` → 网关 8010; 工具走 xiaozhi 8003 /api/push | ✅ |

机器人任务执行方式: `agent_query` 在 agent 自己的可见窗口执行
（Codex-Asong / ClaudeCode-Asong / Antigravity-Asong / pi-Asong, 脚本在 `gateway/state/visible_runs/`）,
结果经 hooks 回流 → 机器人唤醒后 `agent_pending` 播报。

## 五、智能体 Prompt（阿松 v2）

- 文件: `fusion.firmware.0731/prompt-阿松-v2.md`（已贴入 xiaozhi.me 控制台）
- 核心规则: 唤醒优先（每次唤醒先 agent_pending, 逐条播报, clear=true）;
  「查询 XX 状态」→ agent_status（**绝不 agent_query**）; 「让 XX 做事」→ agent_query;
  语音朗读要求（无 markdown/emoji）; LED 固件自动跟随。

## 六、2026-08-03 已修复的问题（防止复发）

1. **codex 沙箱 Access denied**: `~/.codex/config.toml` `[windows] sandbox` `elevated→unelevated`; agents_core/fusion_gateway 去掉 `--sandbox workspace-write`。
2. **agent_status 超时**: agents_core 探测缓存 120s + 4 agent 并发（13.9s→4.8s, 缓存后 <1s）。
3. **中文乱码**: hook 脚本（codex_hook/claude_hook）改读 stdin.buffer + UTF-8; mcp_pipe 子进程 `PYTHONUTF8=1`。
4. **陈旧 outbox**: `agent_result_check` 只返回 30 分钟内新结果, 过期自动归档; 旧文件已清空。
5. **托盘双图标**: fusion_tray.ps1 加单实例保护。
6. **计划任务弹窗**: 全部改 wscript.exe + VBS 隐藏启动（StackChan 3 个任务）。
7. **mcp-endpoint health key**: config.json 已改为容器实际 key `22e242a3ba4e4eaaa02c924c6fc9ded7`。
8. **claude 可见窗口无完成事件**: `claude -p`(print 模式)不触发 Claude Code hooks →
   `agents/claude_visible_run.py` 包装脚本运行并捕获输出, 完成后同时 POST done 到网关
   + 写 outbox（agent_result_check 与 agent_pending 两条路都通）。
9. **claude/pi 可见窗口工作目录**: 从用户主目录改为项目目录 `D:\ProcessCenter\StackChan`
   （与 codex/agy 一致, 「总结当前项目」才有上下文）。
10. **托盘队列操作菜单**: 新增「队列操作」子菜单——显示队列消息内容 / 清空队列
    （自动备份, UTF-8 无 BOM）/ 清空待确认问题。
11. **claude -p 经 cc-switch 非流式输出**: 大任务窗口几十分钟无输出属正常,
    完成后一次性显示; cc-switch 当前路由为 Kimi coding plan（机器人语音链路的
    DeepSeek 是另一条, 互不相干）。

## 七、待办

- [x] Phase 4 完整验收: 唤醒播报 ✅ / 状态查询 ✅ / 任务闭环 ✅
      （codex、claude 总结项目 → 窗口执行 → 唤醒/主动问结果均播报成功, 2026-08-03 晚实测）
- [ ] Phase 5 可选: 云端空闲自查（非打断）; 桌面应用会话注入（等 codex remote-control 稳定）
- [ ] 机器人目前可能待机, 需唤醒后再验证
- [ ] 重新启用 auth（MAC 白名单空 token bug, 可选）
- [x] 2026-08-04 修: 电脑重启后云桥接不自启导致机器人离线——已注册
      StackChan-CloudBridge 登录自启任务(run_bridge_hidden.vbs), install_autostart.ps1 同步。

## 八、常用命令

```powershell
# 网关
powershell -ExecutionPolicy Bypass -File D:\ProcessCenter\StackChan\fusion.firmware.0731\gateway\run_gateway.ps1
# 云桥接
powershell -ExecutionPolicy Bypass -File D:\ProcessCenter\StackChan\fusion.firmware.0731\xiaozhi-mcp\run_bridge.ps1
# 连通性验证（云链路口径）
python D:\ProcessCenter\StackChan\fusion.firmware.0731\scripts\verify_connectivity.py
# 打包
python D:\ProcessCenter\StackChan\fusion.firmware.0731\package_stackchan.py
# 刷机（app-only）
python -m esptool --chip esp32s3 -b 460800 --port COM8 --before default-reset --after hard-reset write-flash 0x410000 xiaozhi.bin
```

## 九、关键文件路径

- 网关: `fusion.firmware.0731/gateway/`（fusion_gateway.py, agents_core.py, fusion_tray.ps1）
- 桥接: `fusion.firmware.0731/xiaozhi-mcp/`（server.py, mcp_pipe.py）
- 固件: `fusion.firmware.0731/firmware/post-fw-v1.0.2-micfix/`
- 提示词: `fusion.firmware.0731/prompt-阿松-v2.md`
- 发布副本: `D:\ProcessCenter\StackChan\stackchan-fusion-github`
- 07.31 备份包: `fusion.firmware.0731/package-stackchan.zip.0731-backup`
