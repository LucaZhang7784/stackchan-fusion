# 接入本地 TRAE Work 可行性研究报告

- 日期：2026-08-07
- 状态：待审核（未动任何代码）
- 目标：将本地 TRAE Work 接入 StackChan 融合体系，功能对标 codex（语音派发任务）与 antigravity（hook 事件回流播报）

## 一、结论

**完全可行，且是三条彼此独立的接入路径：**

- **路径 A（对标 codex）**：TraeCode CLI（`traecli`）作为 Agent CLI 接入网关 `gateway/agents_core.py`，机器人语音派发任务 → 无头执行 → hook 上报 → 桌头播报。
- **路径 B（对标 antigravity）**：TraeCode Hooks 事件回流，写 `agents/trae_hook.py` 挂在 `%userprofile%/.trae-cn/hooks.json`，`Stop` / `Notification` / `PreToolUse` 三事件对齐现有 done / question / progress 三态。
- **路径 C（TRAE Work 定时任务增强）**：Work 模式定时任务完成后通知机器人。官方无原生 webhook，需任务内调用 HTTP/MQTT 或 Skill 实现（待实测）。

本机**已有 TRAE Work（CN）客户端存量环境**，只缺 `traecli` 二进制与 hook 脚本，不是从零立项。

## 二、本机现状（实测证据）

| 项 | 证据 |
|---|---|
| TRAE SOLO CN 客户端已安装 | `C:\Users\zhang.luca\AppData\Local\Programs\TRAE SOLO CN\TRAE SOLO CN.exe`（ProductVersion 0.1.43 / FileVersion 2.3.62834，北京引力弹弓科技，字节系） |
| 客户端内含 Work / Code / Design 三模式 | `.trae-cn\builtin\` 下有 `work`、`code`、`design` 三套 builtin；`.trae-cn\work\` 有 7 月 22–25 日真实会话（水印检测脚本 detect_watermark.py / detect2.py + frame_sample.jpg） |
| 曾启用 Claude Code 系插件 | `.trae-cn\plugin-config.json` 启用了 `wt-agent-hooks`、`superpowers`、`karpathy-skills`、`ponytail`（本地兼容版）；其中 **wt-agent-hooks 即 Antigravity 同款 hook 扩展** |
| 已配置 MCP 与用户 Skill | `.trae-cn\mcps\solo_design_lite`、`.trae-cn\skills\credit-assessment-reporter`（user_upload） |
| **traecli 未安装** | `Get-Command traecli` 无结果；全盘 `%USERPROFILE%`、`%LOCALAPPDATA%`、`%APPDATA%` 深扫无 `traecli*` / `trae-cli*` |
| **hooks.json 尚未配置** | `.trae-cn` 根目录仅 argv.json / installed-plugins.json / plugin-config.json / skill-config.json，无 hooks.json |
| 旧版 SOLO CLI 存在但形态不对 | `bin\trae-solo-cn.cmd`（v1.107.1 / SOLO CN 0.1.43）是 VSCode 系 CLI；`chat` 子命令只能拉起 IDE 窗口（`-m ask|edit|agent`），**不是无头模式**；无 `-p`、无 `acp` 子命令 |

## 三、能力对标表

| 能力 | codex（现状） | antigravity（现状） | Trae 接入后 |
|---|---|---|---|
| 语音派发任务 | `AGENT_CLIS` + `query()` → `codex exec` | 无（仅 hook 回流） | **A：`traecli` 非交互模式** |
| 任务完成播报 | codex_hook → done | antigravity_hook（Stop / AfterAgent / agent.stop / SessionEnd）→ done | **B：Stop / Notification(idle_prompt) → done** |
| 待确认提醒 | codex_hook → question | PermissionRequest → question | **B：Notification(permission_prompt / ask_user_question) → question** |
| 进度/通知 | progress | Notification → progress | **B：Notification → progress** |
| 权限拦截 | PreToolUse 判定 | PermissionRequest 判定 | **B：PreToolUse → permissionDecision allow/deny/ask** |
| 定时任务自动化 | 无 | 无 | **C：Work 定时任务（云端/本地运行）** |

## 四、路径 A：TraeCode CLI 派发（对标 codex）

### 前置条件（安装 + 登录）

```powershell
irm https://trae.cn/trae-cli/install_v2.ps1 | iex
$env:TRAECLI_PERSONAL_ACCESS_TOKEN = "<PAT>"
traecli login --with-trae-pat
```

官方依据：
- [TraeCode CLI 2.0 快速开始](https://docs.trae.cn/cli_get-started-with-trae-code-cli-2)：Windows 安装脚本、PAT 登录、`traecli "任务"` 带入首条任务
- [CLI 登录令牌](https://docs.trae.cn/cli_login-token)：企业版 PAT + 自定义域 `TRAECLI_HOST`
- [TRAE CLI 非交互模式](https://docs.volcengine.com/docs/86677/2227866?lang=zh)：`-p / --print` 打印响应并立即退出，适用管道/CI；`--query-timeout` 限制单次查询时长
- [TRAE 概览](https://docs.trae.cn/)：TRAE CLI 支持非交互模式，可嵌入自动化脚本或 CI/CD

### 网关改动（`gateway/agents_core.py`）

- `AGENT_CLIS["trae"] = {"cli": "traecli", "exec_args": ["-p"], "version_args": ["--version"], ...}`
  - **`-p` 旗标需装完后 `traecli --help` 实测确认**（2.0 版若调整则对齐）
- `AGENT_ALIASES` 增加：`"trae"→"trae"`、`"trae work"→"trae"`、`"特拉"→"trae"`、`"trei"→"trae"` 等
- `probe()`：`traecli --version` + 进程快检；未安装/未登录时 5ms 级 Fail-Fast 返回提示

## 五、路径 B：TraeCode Hooks 回流（对标 antigravity）

### 配置文件（官方路径见 [Hook 配置详解](https://docs.trae.cn/ide_hook-configuration-reference)）

全局：`%userprofile%/.trae-cn/hooks.json`（Windows）；项目：`$PROJECT/.trae/hooks.json`

```json
{
  "version": 1,
  "hooks": {
    "Stop": [
      { "hooks": [{ "type": "command", "command": "python ${USERPROFILE}/ProcessCenter/StackChan/fusion.firmware.0731/agents/trae_hook.py", "timeout": 30 }] }
    ],
    "Notification": [
      { "matcher": "idle_prompt|permission_prompt|ask_user_question", "hooks": [{ "type": "command", "command": "python .../trae_hook.py", "timeout": 30 }] }
    ],
    "PreToolUse": [
      { "matcher": "RunCommand|Write|Edit", "hooks": [{ "type": "command", "command": "python .../trae_hook.py", "timeout": 30 }] }
    ]
  }
}
```

### 事件映射（`agents/trae_hook.py`，复刻 antigravity_hook.py 结构）

从 stdin 读 JSON → POST `http://127.0.0.1:8010/api/agent_event`（Bearer auth）

| TraeCode 事件 | stdin 关键字段 | 映射 |
|---|---|---|
| Stop | `last_assistant_message`（最终输出文本）、`session_id`、`loop_count` | → done（摘要为空兜底"Trae 任务结束"） |
| Notification `idle_prompt` | `message` | → done / progress |
| Notification `permission_prompt` / `ask_user_question` | `message`、`tool_use_id` | → question（"Trae 需要确认: …"） |
| PreToolUse | `tool_name`（RunCommand/Write/Edit…）、`tool_input` | → question；回 `permissionDecision: allow/ask` 不阻断主流程 |

`msg_uid` 沿用现有规范：`trae_{session_id}_{turn_hash}`，网关幂等去重直接复用。

### 已核实的关键官方细节

- 6 类事件：SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / Notification（无 SessionEnd；Stop 即"任务完成输出"）
- Notification 为**异步事件，不阻塞主流程**；`notification_type` 支持 `idle_prompt`（任务完成）、`permission_prompt`（需确认）、`ask_user_question`、`document_review`、`browser_interaction`
- PreToolUse 的 `permissionDecision`：`allow | deny | ask`（多个 Hook 并行时优先级 deny > ask > allow）
- Windows 下 Hook 命令**默认 PowerShell**；环境变量含 `TRAE_PROJECT_DIR` / `CLAUDE_PROJECT_DIR`
- 退出码：0 正常、2 阻断性错误、其他非阻断
- TraeCode **支持读取 Claude Code hooks（~/.claude/settings.json）并合并执行** → 见风险 1

## 六、路径 C：TRAE Work 定时任务通知（增强，暂缓）

[Work 定时任务文档](https://docs.trae.cn/work_automated-tasks)确认：
- 触发方式：固定时间 / 间隔 / 自然语言自定义
- 运行模式：Work / Code；运行环境：**云端 / 本地**；可指定输出存储位置
- 创建方式：模板 / 手动 / 对话中创建

**官方未提供任务完成 webhook**。可行替代：
1. 任务内容末尾明确指示 AI 调用 HTTP 工具 POST 到网关（本地运行环境可访问 `127.0.0.1:8010`；云端沙箱需公网入口，如 Tailscale Funnel 或直发 MQTT broker-cn.emqx.io）
2. 写 `stackchan-notify` Skill 装进 `.trae-cn/skills/`（已有 credit-assessment-reporter 先例）
3. 待验证：Work 执行历史是否有可轮询 API

## 七、风险与待验证项（按严重度排序）

1. **TraeCode 合并执行 `~/.claude/settings.json` hooks**（官方明确"同时启用 Claude Code Hook 和 TraeCode Hook 时合并执行"）。本机 Claude hooks 已配 4 个钩子，TraeCode 任务结束会**额外触发 claude_hook.py**，存在双报风险。对策：TraeCode 设置关闭"导入 Claude 中的 Hooks"，或靠网关 msg_uid 去重（需实测两脚本 uid 是否一致）。
2. **`-p/--print` 非交互旗标**：火山引擎文档确认存在，但 TraeCode CLI 2.0 是否保留需装完实测；兜底为 `traecli acp serve`（[ACP 文档](https://docs.trae.cn/cli_agent-client-protocol)），ACP 是长驻协议，接入成本高，不建议首期。
3. **Windows hook shell 是 PowerShell**：现有 hooks 多为 cmd/bash 写法；路径含空格须引号包裹；trae_hook.py 按 TraeCode 字段解析，不能直接复用 claude_hook.py（官方已提示同名事件输入输出可能不同）。
4. **登录与沙箱**：Windows 原生版首次启动需完成沙箱初始化；PAT 需 Trae 账号后台生成；未登录时 `probe()` 需识别并 Fail-Fast。
5. **Work 云端沙箱无法访问本地网关**：云端任务通知只能走公网/MQTT；本地运行无此问题。
6. **TRAE Work 桌面版与已装 TRAE SOLO CN 的关系**：TRAE Work 由 TRAE SOLO 升级而来（2026-06-09），桌面版独立于 TRAE IDE；本机客户端已含 work/code/design builtin，等价于 Work 客户端存量版本。

## 八、分阶段实施建议

| 阶段 | 内容 | 交付物 | 预计量 |
|---|---|---|---|
| 1（推荐先做） | 安装 traecli + PAT 登录；写 trae_hook.py + 全局 hooks.json；真机验证"Trae 任务结束 → 机器人播报" | 路径 B 闭环 | Python ~120 行 + 1 json |
| 2 | agents_core 注册 trae 别名与派发；验证 `-p` 非交互 | 路径 A 闭环 | Python ~30 行 |
| 3 | Work 定时任务 + stackchan-notify Skill（本地运行） | 路径 C | Skill 1 个 |
| 4（可选） | ACP 集成、多 Agent 混音去重观察 | 增强 | 待定 |

阶段 1 不动现有 codex/antigravity 链路，风险最低，且能立刻验证"对标 antigravity"的核心体验。

## 九、关键引用

- [TraeCode CLI 2.0 快速开始](https://docs.trae.cn/cli_get-started-with-trae-code-cli-2)
- [Hook 配置详解](https://docs.trae.cn/ide_hook-configuration-reference)
- [通过 Hook 实现自动化](https://docs.trae.cn/ide_automate-actions-with-hooks)
- [Work 定时任务](https://docs.trae.cn/work_automated-tasks)
- [TRAE CLI 非交互模式（火山引擎）](https://docs.volcengine.com/docs/86677/2227866?lang=zh)
- [Agent Client Protocol (ACP)](https://docs.trae.cn/cli_agent-client-protocol)
- [TRAE Work 客户端上线](https://docs.trae.cn/ide_trae-solo-is-now-available)

