# StackChan 融合项目记忆（权威版）

> 更新: 2026-08-07 晚 (+08:00) · v08.09 · 会话开始时先读本文件再动手。

## 一、当前架构（云链路 + 唤醒播报）

```
机器人(M5Stack CoreS3, 固件 v1.2-mqttpush)
  │ 语音走 xiaozhi.me 云端 STACK 智能体(ASR/LLM/TTS 全在云端)
  ▼
xiaozhi.me 云 LLM ──MCP──► xiaozhi-mcp 桥接(mcp_pipe.py + server.py, 本机)
                              │ agent_status / agent_query / agent_pending /
                              │ agent_confirm / agent_result_check / robot_snap / docker_status ...
                              ▼
                        融合网关 fusion_gateway.py (:8010, 13 工具, 含 local_query)
                              │
        ┌─────────────────────┼──────────────────────┐
   codex(CLI/桌面, hooks)  claude(hooks+确认回环)  agy/Antigravity(fusion hooks)  pi(扩展)
   （机器人任务在 agent 自己的可见窗口执行, 结果经 hooks 回流网关→机器人唤醒播报）
```

- **主链路**: 云链路; 自建 docker xiaozhi-esp32-server + Tailscale Funnel 只作备用。
- **主动播报方式**: EMQX MQTT µ-law 直推（`stackchan/{mac}/push`）+ msg_uid 幂等 +
  固件 ACK 点杀（v08.06 起）; 离线消息保留 pending, 唤醒后 `agent_pending` 补播;
  唤醒优先规则仍保留（每次唤醒先念待播报）。
- **唤醒词「阿松」**（拼音 a song, 固件硬编码 + 你好小智兜底; 阈值下限 0.30, 检测窗口 1500ms）。
- **后台预热连接**: 待机时维持一条 WebSocket 连接（曾连上后掉线 2s 内秒连; 首次失败 5s→40s 退避）, 唤醒时跳过重新握手。

## 二、服务状态（2026-08-03 实测）

| 服务 | 端口 | 状态 |
|---|---|---|
| 融合网关 fusion_gateway.py | 8010 | ✅ /healthz ok, 12 工具（含 robot_snap） |
| xiaozhi-mcp 云桥接 (mcp_pipe + server.py) | — | ✅ 心跳 60s 正常; 开机自启任务 StackChan-CloudBridge(wscript 隐藏) |
| xiaozhi-esp32-server (Docker, 备用) | 8000/8003 | ✅ Up healthy |
| mcp-endpoint-server (Docker) | 8004 | ✅ Up healthy |
| xiaozhi web / redis / db | 8002/6379/3306 | ✅ Up |
| funnel_proxy.py (备用路由) | 8090 | ✅ 运行中 |
| Tailscale Funnel | 443 | ✅ https://YOUR_FUNNEL_DOMAIN.ts.net |
| 系统托盘 fusion_tray.ps1 | — | ✅ 单实例（有保护） |

## 三、机器人固件（重要）

- **当前固件**: `firmware/post-fw-v1.2-mqttpush`（已刷入, v08.06 起）
  - 基座: **07.31 已跑通的** `reference/stackchan-xiaozhi-firmware`（heavenchenggong 系, 含「阿松」+ LED 补丁, 不要用 HtSz 主分支——有 bug 起不来）
- **v1.2-mqttpush 能力**: 第二条 MQTT 推送链路（µ-law 直出播放、msg_uid 解析 +
    ACK 回执、keepalive 15s、MQTT buffer 8KB、poll 超时 5s、lwIP 收窗口 16KB、
    播报期关 WiFi 节能、SSID 智能路由）; **Phase 8.1** done→Nod / question→TiltAsk(+15°),
    待机摆头 20s; **Phase 8.2** 拍照 JPEG 分块 MQTT（`stackchan/{mac}/photo` QoS1）。
  - **v08.07 LED 根治补丁（真机确认播报变绿→待机暖橙）**:
    **真正根因 = PY32 GPIO13 未初始化**——对照 M5Stack 出厂固件
    (hylarucoder-StackChan hal_io_expander): 灯环 WS2812×12 驱动前必须把 PY32
    GPIO13 配为 输出+上拉+推挽（REG_GPIO_M_H=0x04 / PU_H=0x0A / PD_H=0x0C /
    DRV_H=0x14 的 bit5）。此前缺这步, 0x24/0x30 写入被 PY32 接受但灯环不驱动,
    "写成功但灯不变"。另补: ① `Py32WriteRegBlock` 失败日志 + 一次重试;
    ② `led_manual_` 仅待机生效/活跃态强制状态色/离开自动复位暖橙/Idle 待机锁;
    ③ `i2c_bus_mutex_`（FreeRTOS Mutex + 50ms 超时）+ 触屏 I2C 故障冷却 +
    400ms 防踩踏；④ refreshLeds 改为读-改-写（保留 CFG 其他位）。
  - v1.0.6（历史）: TTS 播放缓冲——解码队列 2.4s→4.8s、播放余量 2→4 帧、
    入队改**背压不丢包**——针对长播报吞字。
  - v1.0.5: 麦克风增益 42→36（42 可能削波导致"播报队列消息"→"播放对你秋田"式失真）。
  - v1.0.4: **设备端 AEC 回退**——v1.0.3 开的 AEC 在 CoreS3 上会让 `audio_input` 任务
    在 dios_ssp AEC DSP（`complex_abs2`）里死循环 → task_wdt 触发、机器人无反应/重启;
    已恢复 VAD(WebRTC) 管线（AEC 方案废弃, 详见 Pending）。
  - v1.0.3（已废弃）: 启用设备端 AEC + 唤醒加速 + 后台预热连接。
  - 保留至今的优化: 唤醒加速（multinet 窗口 3000→1500ms、阈值下限 0.35→0.30）;
    后台预热 WS 连接（曾连上掉线 2s 秒连）; `OpenAudioChannel(silent)` 后台失败不弹错误。
  - 上一版跑通基线 v1.0.2-micfix: 麦克风增益 42; 唤醒词 阿松; 阈值下限 0.35
  - 布局: post-fw（app @ 0x410000, 16MB）; app-only 刷 `xiaozhi.bin @ 0x410000` 保留配置
  - 构建: espressif/idf:v5.5.2（5.5.4 会黑屏）, `firmware/build_led_fw.ps1` / `build_led_ci.sh` 流程
  - 构建脚本: `firmware/build_fw_v10{3,4,5,6}.ps1`; 当前刷机目录 `firmware/post-fw-v1.0.6-ttsbuf/`
- 麦克风增益文件: `reference/stackchan-xiaozhi-firmware/main/boards/m5stack-core-s3/cores3_audio_codec.cc` (`input_gain_`)
- 唤醒词: 固件 `custom_wake_word.cc` 硬编码 `{"a song","阿松","wake"}` + 你好小智兜底

## 四、四 agent 接入要点

| Agent | 方式 | 状态 |
|---|---|---|
| codex | `~/.codex/hooks.json`(5 事件→codex_hook.py, v08.07 清掉 PromLight 僵尸钩子) + `config.toml` `bypass_hook_trust=true` + `[windows] sandbox='unelevated'` | ✅ 桌面+CLI 都上报 |
| claude | `~/.claude/settings.json` hooks→claude_hook.py（v08.09 起主存 settings.json: Windows 2.1.x 的 settings.local.json 有 #64699 静默失效 BUG; ccswitch 覆盖后托盘「安装/修复 Claude Hooks」自愈）+ confirm_mcp.py（确认回环完整） | ✅ |
| agy/Antigravity | `~/.gemini/config/hooks.json` **标准结构**（顶层 `hooks` 键 + matcher + hooks[command]，2026-08-07 15:46 根治: 命名空间结构会被语言服务器整体拒绝）; 事件名用桌面版支持的 SessionStart/PreToolUse/PostToolUse/PermissionRequest/PermissionDenied/Elicitation/Stop; CLI 按 artifactDirectoryPath 含 antigravity-cli 归属 agent=agy | ✅ |
| pi | `~/.pi/agent/extensions/hooks-bridge.ts` → 网关 8010; 工具走 xiaozhi 8003 /api/push | ✅ |

机器人任务执行方式: `agent_query` 在 agent 自己的可见窗口执行
（Codex-Asong / ClaudeCode-Asong / Antigravity-Asong / pi-Asong, 脚本在 `gateway/state/visible_runs/`）,
结果经 hooks 回流 → 机器人唤醒后 `agent_pending` 播报。

## 五、智能体 Prompt（阿松 v3）

- 文件: `prompt-阿松-v3.md`（v3.5: LLM=DeepSeekLLM(deepseek-v4-flash), TTS=EdgeTTS
  zh-HK-HiuGaaiNeural 粤语女声; 需贴入 xiaozhi.me 控制台; v2 保留存档）
- 核心规则:
  - **回复语言跟随 xiaozhi.me 智能体/音色预设**（预设粤语就用粤语，预设普通话就用普通话，不强制）
  - 唤醒优先（每次唤醒先 agent_pending, 逐条播报, clear=true）
  - **ASR 容错意图兜底**: 听错时按"意思"推断（不分普通话/粤语/方言）——「播报/消息/队列/待办」→ agent_pending;
    「状态/在干嘛/进程」→ agent_status; 「结果/完了吗」→ agent_result_check;
    「可以/拒绝」→ agent_confirm; 「docker/容器」→ docker_status
  - 禁止把「播报消息/查状态」理解成点歌/搜索; 听不清回「再说一遍」
  - 语音朗读要求（无 markdown/emoji）; **超过 50 字先摘要再播报**; 拍照铁律
    （说「看看/拍照」必须立即调 `self.camera.take_photo`）; LED 固件自动跟随

## 六、2026-08-03 已修复的问题（防止复发）

1. **codex 沙箱 Access denied**: `~/.codex/config.toml` `[windows] sandbox` `elevated→unelevated`; agents_core/fusion_gateway 去掉 `--sandbox workspace-write`。
2. **agent_status 超时**: agents_core 探测缓存 120s + 4 agent 并发（13.9s→4.8s, 缓存后 <1s）。
3. **中文乱码**: hook 脚本（codex_hook/claude_hook）改读 stdin.buffer + UTF-8; mcp_pipe 子进程 `PYTHONUTF8=1`。
4. **陈旧 outbox**: `agent_result_check` 只返回 30 分钟内新结果, 过期自动归档; 旧文件已清空。
5. **托盘双图标**: fusion_tray.ps1 加单实例保护。
6. **计划任务弹窗**: 全部改 wscript.exe + VBS 隐藏启动（StackChan 3 个任务）。
7. **mcp-endpoint health key**: 见 `gateway/config.json` 的 endpoint_health_url（真实 key 不入库）。
8. **claude 可见窗口无完成事件**: `claude -p`(print 模式)不触发 Claude Code hooks →
   `agents/claude_visible_run.py` 包装脚本运行并捕获输出, 完成后同时 POST done 到网关
   + 写 outbox（agent_result_check 与 agent_pending 两条路都通）。
9. **唤醒后进聆听慢（几秒）**: 双管齐下——multinet 检测窗口 3000→1500ms、阈值下限 0.35→0.30;
   另加后台预热 WS 连接（待机常驻, 断线 15s→120s 退避重连）, 唤醒跳过 DNS/TLS/hello。
10. **TTS 播放时识别差/丢字**: ~~启用设备端 AEC~~ **已回退**——AEC 在 CoreS3 上导致
    audio_input 死循环（见第 12 条）; 现靠云端 ASR + 增益 36。
11. **后台连接失败弹错误提示**: `OpenAudioChannel(bool silent)`——预热连接失败只记日志,
    不触发 MAIN_EVENT_ERROR 弹窗。
12. **设备端 AEC 导致机器人无反应/重启（2026-08-04）**: v1.0.3 开 AEC 后 `audio_input`
    线程在 `dios_ssp_aec_erl_est_process → complex_abs2` 死循环（task_wdt 每 10s 触发）;
    addr2line 定位后回退 AEC（v1.0.4）, 恢复 VAD(WebRTC) 管线, task_wdt=0。**AEC 方案废弃**。
13. **长播报时断时续/吞字（2026-08-04, v08.09 网关侧根治）**: v1.0.6 已加大解码缓冲
    （2.4s→4.8s）+ 播放余量（2→4）+ 入队背压不丢包; v08.09 网关两轨修复——
    MQTT 2帧/批（~1.9KB < MTU, 根治 TCP 分片丢帧）+ LLM ≤60 字口语化摘要
    （18:23:54 221字→50字真机 ACK）。固件侧段边界 `ResetDecoder()` 仍为备份待查项。
14. **Antigravity 桌面重启后仍不播报（2026-08-07 13:29 修复, 防复发）**:
    - 死穴一: `~/.gemini/config/hooks.json` 顶层命名空间被误改为 `"fusion"`, IDE 只识别
      规范命名空间 `"stackchan"` / `"promlight"`, 整段 Hook 被静默忽略 → 已还原为 `"stackchan"`。
    - 死穴二: `agents/antigravity_hook.py` 事件匹配只认 `"Stop"`, 而 IDE 实际上报
      `"AfterAgent"`/`"agent.stop"` → 已升级为
      `elif event in ("Stop", "AfterAgent", "agent.stop", "SessionEnd", "agent.session.end")`。
    - 验收: AfterAgent 模拟报文实测 `posted done ok`; 13:29:16 网关日志
      `push ack [agent]: agy 任务完成` 真机播报+ACK, pending 清空。
    - 约束: 以后改 hooks.json 顶层命名空间**严禁改成 "fusion"**, 必须用 "stackchan"。
- ⚠️ **本条目两个"死穴"结论已被 #16 推翻**（2026-08-07 15:46）: ①顶层命名空间
  （stackchan/promlight）根本不是合法结构, 会导致语言服务器整文件解析失败;
  ②"IDE 实际上报 AfterAgent/agent.stop" 不成立——桌面版语言服务器支持的是
  PreToolUse/PostToolUse/Stop 等事件名, AfterAgent 是 Gemini CLI 的事件名。
15. **LED 灯环常亮橙色、不随状态变化（2026-08-07 根治, 防复发）**:
    - 真根因: PY32 **GPIO13 未配输出推挽**, 灯环数据线无法驱动——
      对照出厂固件 hal_io_expander 初始化序列补齐（0x04/0x0A/0x0C/0x14 bit5）。
    - 加固: `m5stack_core_s3.cc` 补 PY32 写失败日志+重试、refreshLeds 读-改-写、
      手动色仅待机生效/活跃态强制状态色/离开自动复位暖橙/Idle 待机锁、
      `i2c_bus_mutex_`(50ms)+触屏故障冷却+400ms 防踩踏。
    - 真机: 播报变绿、结束回暖橙, 无 I2C 报错。聆听蓝待唤醒验证。
    - Prompt 保固: 阿松 v3.6 严禁 LLM 调 `self.led.*` 表达情绪，用户明确要求时
      必须同轮 `self.led.auto` 恢复。LED 颜色永远归固件状态机。
16. **Antigravity hooks 命名空间结构整体失效（2026-08-07 15:46 根治, 推翻 #14）**:
    - 现象: 15:22 Antigravity 桌面崩溃重启后, hook 事件全部不再到达 antigravity_hook.py,
      结论不进队列、机器人不播报（hook 日志最后写入 13:29, transcript 却活动到 15:23）。
    - 铁证: `%APPDATA%\Antigravity\logs\language_server.log` 15:23:27:
      `hooks.go:44 Failed to parse hooks file ~/.gemini/config/hooks.json:
      invalid hook "stackchan": command hook must specify 'command'`
      —— 顶层命名空间（stackchan/promlight）结构导致**整文件解析失败、全部事件被丢弃**。
    - 真相: Antigravity 桌面版语言服务器只认**标准 Gemini hooks 结构**:
      `{"hooks": {"<事件>": [{"matcher": ".*", "hooks": [{"type": "command",
      "command": "..."}]}]}}`; 支持事件名（从 language_server.exe 二进制确认）:
      SessionStart / PreToolUse / PostToolUse / PermissionRequest / PermissionDenied /
      Elicitation / Stop / SessionEnd / Notification。
    - 修复: `~/.gemini/config/hooks.json` 重写为标准结构（7 事件, matcher ".*",
      无 timeout; promlight 段移除, 备份 `hooks.json.bak-20260807-hooksgo`）。
    - 防复发: **严禁再改回命名空间结构**; 改完 hooks.json 必须重启 Antigravity 桌面版,
      并确认 language_server.log 无 "Failed to parse hooks file"。
9. **claude/pi 可见窗口工作目录**: 从用户主目录改为项目目录 `<PROJECT_ROOT>`
   （与 codex/agy 一致, 「总结当前项目」才有上下文）。
10. **托盘队列操作菜单**: 新增「队列操作」子菜单——显示队列消息内容 / 清空队列
    （自动备份, UTF-8 无 BOM）/ 清空待确认问题。
11. **claude -p 经 cc-switch 非流式输出**: 大任务窗口几十分钟无输出属正常,
    完成后一次性显示; cc-switch 当前路由为 Kimi coding plan（机器人语音链路的
    DeepSeek 是另一条, 互不相干）。

## 七、待办

- [x] Phase 4 完整验收: 唤醒播报 ✅ / 状态查询 ✅ / 任务闭环 ✅
      （codex、claude 总结项目 → 窗口执行 → 唤醒/主动问结果均播报成功, 2026-08-03 晚实测）
- [x] Phase 5 决策（2026-08-04）:
  - P5-1（pi 语音确认回环）: **舍弃** —— pi 直接走 VS Code 交互, 不做语音回环。
  - P5-2（agy 语音确认回环）: **舍弃** —— agy 直接走 Antigravity Desktop 交互。
  - P5-4（云端空闲自查/主动推送）: **不可行** —— xiaozhi.me 云智能体只在语音对话时触发,
    无空闲/定时自触发 API; 真·主动播报只能走自建链路 robot_say（已有）。
  - P5-5（桌面应用会话注入）: **不可行** —— `codex app-server daemon` 仅支持 Unix;
    `codex remote-control` 是 SSH/移动配对机制, 不是桌面会话注入 API。
    维持现状: 机器人任务在 CLI 可见窗口执行, 桌面应用会话经 hooks 上报。
- [x] 2026-08-04: 固件 v1.0.6-ttsbuf 刷入（AEC 已回退; 增益 36; TTS 缓冲加大; 唤醒加速+预热连接保留）
- [x] 2026-08-04: xiaozhi.me STACK 智能体模型 `deepseek-v4-flash-ha` 出现
      `503 No available channel` → 控制台已换 `qwen3.6`
- [x] 2026-08-06: 播报链路根治（µ-law + lwIP 窗口 16KB + msg_uid/ACK 幂等闭环）;
      固件 v1.2-mqttpush 刷入; 托盘按云链路口径重写。
- [x] 2026-08-07: Phase 8.1 动作联动（done→Nod/question→TiltAsk, 待机摆头 20s）;
      Phase 8.2 `robot_snap` 拍照 MCP（连拍 3/3）; Claude hooks 迁移
      `settings.local.json`（五工单: 抗 ccswitch 覆盖 / VS Code 拒发 / 空摘要兜底 /
      codex hooks 清 PromLight 僵尸）; 本地存档 `version.08.07/` + GitHub 同步。
- [x] 2026-08-07 晚 v08.09: 网关两轨吞字修复（MQTT 2帧/批 < MTU + LLM ≤60 字摘要,
      真机 ACK 50 字摘要）; Claude hooks 迁移 settings.json（#64699, 安装脚本 5.1 兼容）;
      托盘「安装/修复 Claude Hooks」自愈菜单; config local_llm_model→qwen3.5:9b;
      本地存档 version.08.09/ + GitHub 脱敏同步。
- [ ] **Pending · 长播报时断时续/吞字**: 用户反馈长语音播报仍时断时续（v1.0.6 缓冲加大
      后未根治）。排查线索: ①多段 TTS 时 `kDeviceStateSpeaking` 每次进入都调
      `ResetDecoder()` 清解码/播放队列; ②服务器突发推送导致欠载; ③WS 断流。
      验证方法: 串口抓长播报, 观察段边界状态切换与播放连续性。
- [ ] 机器人目前可能待机, 需唤醒后再验证
- [ ] 重新启用 auth（MAC 白名单空 token bug, 可选）
- [x] 2026-08-04 修: 电脑重启后云桥接不自启导致机器人离线——已注册
      StackChan-CloudBridge 登录自启任务(run_bridge_hidden.vbs), install_autostart.ps1 同步。
- [x] 2026-08-04: 托盘新增 Restore-BridgeIfDown 守护(进程<2 或心跳>3分钟静默重启,
      防抖 30s), 桥接实现「开机自启 + keep alive」(桥接需直接操作 Windows 宿主,
      无法进 Linux Docker)。
- [x] 2026-08-04: 托盘单实例守卫加固——只匹配 `-File ...fusion_tray.ps1` 的真实
      托盘实例, 避免命令行仅提及路径的进程被误判而 exit 0。

## 八、常用命令

```powershell
# 网关
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\gateway\run_gateway.ps1
# 云桥接
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\xiaozhi-mcp\run_bridge.ps1
# 连通性验证（云链路口径）
python <PROJECT_DIR>\scripts\verify_connectivity.py
# 打包
python <PROJECT_DIR>\package_stackchan.py
# 刷机（app-only）
python -m esptool --chip esp32s3 -b 460800 --port COM8 --before default-reset --after hard-reset write-flash 0x410000 xiaozhi.bin
```

## 九、关键文件路径

- 网关: `<PROJECT_DIR>/gateway/`（fusion_gateway.py, agents_core.py, fusion_tray.ps1）
- 桥接: `<PROJECT_DIR>/xiaozhi-mcp/`（server.py, mcp_pipe.py）
- 固件: `<PROJECT_DIR>/firmware/post-fw-v1.2-mqttpush/`; 源码 `reference/stackchan-xiaozhi-firmware-mqtt`
- 提示词: `<PROJECT_DIR>/prompt-阿松-v3.md`
- 发布副本: `<GITHUB_REPO_DIR>`（stackchan-fusion-github）
- 07.31 备份包: `<PROJECT_DIR>/package-stackchan.zip.0731-backup`


## 十、Hooks 保固规范（v08.10.2，务必遵守）

- **Antigravity hooks.json = 扁平结构**：`~/.gemini/config/hooks.json` 的 stackchan 段，
  每个事件条目顶层直接写 `{"type":"command","command":"...","timeout":10}`；
  嵌套 `{"hooks":[...]}` 会被 Antigravity 桌面端 Go 语言服务器拒载
  （`language_server.exe` / `hooks.go:44 "command hook must specify 'command'"`）。
- **Claude / Codex = 嵌套结构**：`~/.claude/settings.json` 与 `~/.codex/hooks.json`
  用嵌套 `{"hooks":[...]}`（各自 loader 认嵌套），不要拍平。
- **唯一自动修复方是 `scripts/hook_health.py`**：每 30 分钟周期自检 + 托盘
  「Hook 自检与修复」菜单；修复前强备份 `.bak-auto-repair-*`；异常/已修复会以
  `agent=system` 推送机器人告警。
- **心跳防误杀**：Antigravity 进程在运行 + language_server.log 近期有活动、
  但 antigravity_hook.log 超过 6 小时无写入才告警；隔夜挂机不告警。
- 历史教训：2026-08-10 曾因 Antigravity 把 stackchan 段"修"成嵌套/被 Agent 拍平，
  导致钩子静默失效 2 天（日志零写入、消息不播报）。任何 Agent 声称"修复 hooks"时，
  先核对本条规范。


