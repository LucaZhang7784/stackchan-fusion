# StackChan × Claude Code 耦合审计与修复工单

- **审计身份**：代码审计员（只读，未修改任何项目代码）
- **审计日期**：2026-08-07
- **审计对象**：codex thread `019fd205-2729-7cc3-be50-8299e6fb0d51` 及本机落盘文件
- **取证来源**：`<PROJECT_DIR>\`、`~/.codex/`、`~/.claude/`、gateway 实时日志、`hooks.json`、`config.toml`
- **实施方**：codex（审计员不下场改代码，只验收证据）

---

## 第一部分：耦合与功能实现审计

### 1.1 设计意图的 Claude Code 耦合链

```
Claude Code (CLI / VS Code 插件)
  └─ hooks(Stop/SessionEnd/Notification/PermissionRequest) ──> ~/.claude/settings.json
       └─ claude_hook.py ──POST──> fusion_gateway:8010/api/agent_event
            └─ pending_append + _enqueue_push ──MQTT──> 机器人播报
反向：机器人语音 → LLM → agent_query("claude") → agents_core.spawn_visible("claude")
       → claude_visible_run.py（可见窗口执行，结果经 hooks 回流）
```

### 1.2 实测的链路状态

| 环节 | 文件 | 实测状态 |
|---|---|---|
| gateway `/api/agent_event` 处理 | `fusion_gateway.py:921-975` | ✅ 正确：question/done/error 都做了 `pending_append + _enqueue_push`，msg_uid 幂等 |
| claude_hook.py 脚本 | `agents/claude_hook.py` | ✅ 脚本本身写得到位（msg_uid 去重、PermissionRequest→question、transcript_path 兜底） |
| **~/.claude/settings.json hooks 注入** | `~/.claude/settings.json` | ❌ **完全缺失 hooks 段**（见 §2 发现 1） |
| agents_core VS Code 可见执行 | `agents_core.py:274-283 VISIBLE_SPECS` | ❌ **无 vscode 项**（见 §2 发现 2） |
| vscode_hook.py 自动钩子 | `agents/vscode_hook.py` | ⚠️ 仅手动触发，无自动 hook |
| codex 侧耦合 | `~/.codex/hooks.json` + `codex_hook.py` | ✅ 在线（`codex_hook.log` 今日 10:33 仍在写入） |

### 1.3 功能实现矩阵

| 功能 | Claude Code CLI | VS Code Claude Code 插件 | VS Code 通用任务 |
|---|---|---|---|
| Agent 状态查询（机器人→agent） | ✅ agents_core probe | ✅（同 CLI） | ✅（同 CLI） |
| 语音驱动任务执行（机器人→agent） | ✅ spawn_visible claude | ✅（同 CLI） | ❌ 无 VISIBLE_SPECS，`code -r` 退化 |
| 任务完成 → 机器人播报 | ❌ hooks 缺失 | ❌ hooks 缺失 | ❌ 无自动 hook |
| 权限请求 → 机器人播报 | ❌ hooks 缺失 | ❌ hooks 缺失 | n/a |
| 语音确认回复 → agent | ✅ confirm_mcp + `--permission-prompt-tool` | ⚠️ VS Code 下未实测 | n/a |

---

## 第二部分：核心问题（按严重度排序）

### 🔴 发现 1（CRITICAL）：Claude Code hooks 根本没接上 —— "机器人不播报"的直接根因

**铁证**：

- `~/.claude/settings.json` 实测 484 字节，修改时间 **2026-08-07 10:29:49**，内容只有 `env` + `includeCoAuthoredBy` 两个键，**没有 `hooks` 键**。
- 全盘扫描 5 个 claude settings 文件（`settings.json` / `settings.local.json` / `settings - Copy.json` / 项目级 `.claude/settings.json` / `.claude/.claude/settings.local.json`），**无一包含 hooks**。
- `install_claude_hooks.ps1` 存在且逻辑正确（标准 `matcher + hooks[]` 结构），但其写入结果**被后续进程覆盖**。
- `claude_hook.log` 最后写入 10:16（早于 10:29 的覆盖），其中 `claude-vsc-repro` 是 codex 手动测试的假 session，**不是真实耦合证据**。

**覆盖元凶**：`settings.json` 的 `env` 段是 ccswitch 模型切换代理（`ANTHROPIC_BASE_URL=http://127.0.0.1:15721`）写入的。ccswitch 每次切模型都会用"只含 env"的 JSON 全量覆盖 settings.json，**把 hooks 段彻底抹掉**。这是一次性安装脚本对抗不了的结构性回归——用户自己的全局 CLAUDE.md 里写了 "On session start, check if ~/.claude/settings.json has valid hooks structure"，证明这个回归**已经反复发生过**。

**影响**：

- Claude Code CLI 与 VS Code Claude Code 插件**共用同一个 `~/.claude/settings.json`**，因此**两者同时失联**。
- Stop / SessionEnd / Notification / PermissionRequest 四个事件**全部不触发** `claude_hook.py` → 网关收不到 done/question → 机器人永远不播报 Claude 的任务完成/待确认。
- 用户最后一条反馈 "VS Code terminal 报 Response stalled mid-stream，机器人没有播报"——**直接命中此根因**。

### 🔴 发现 2（HIGH）：VS Code 耦合结构性残缺

- `agents_core.py:274` `VISIBLE_SPECS` 只有 `codex/claude/agy/pi`，**没有 `vscode`**。语音说"让 vscode 做某事"时，`spawn_visible("vscode", task)` 返回"未知 agent: vscode"，回退到 `run_agent` → 执行 `code -r "<task文本>"`。这会把**任务文本当作文件路径打开**，而不是执行任务。**语音驱动 VS Code 形同虚设**。
- `vscode_hook.py` 只能在 tasks.json 末尾手动追加或命令行手动调用，**VS Code 终端/插件任务结束不会自动上报**。log 里 "VS Code 钩子脚本验证" 那条是手动测试，不是自动闭环。
- VS Code Claude Code 插件（`anthropic.claude-code-2.1.223`）本质跑的是 Claude Code CLI，**理论上**受发现 1 同等影响——hooks 缺失就完全不耦合。

### 🟠 发现 3（HIGH）："Response stalled mid-stream" 是上游模型问题，但会静默破坏播报

- 该报错是 LLM 流式响应中断（ccswitch 代理 → glm-5.2/deepseek 上游断流），**不是耦合层 bug**。
- 但响应中断时 Stop hook 可能不触发，或触发时 transcript 不完整 → `claude_hook.py` 即便接上也会摘要为空（"任务已结束(无文本输出)"）。
- 即便接上 hooks，这类中断也需单独兜底（见 §3 工单 4）。

### 🟠 发现 4（MEDIUM）：ccswitch 是 hooks 持久化的结构性威胁

任何对发现 1 的修复，只要 ccswitch 还在用"全量覆盖 env-only JSON"的方式写 settings.json，**下一次切模型就会再次抹掉 hooks**。不解决 ccswitch，修复必然复发。

---

## 第三部分：StackChan-Fusion 与 PromLight 共存性结论

### 3.1 实测足迹

| 维度 | stackchan-fusion | promlight |
|---|---|---|
| docker MCP profile | `stackchan`（已注册，codex config.toml 启用） | `promlight`（**已移除**，`docker mcp profile ls` 仅剩 stackchan） |
| MCP server 文件 | `bridge/stackchan_mcp.js`（在线，工具 `stackchan_check_task` / `stackchan_respond`） | `.opencode/mcp-servers/promlight/`（**已删除**） |
| 计划任务 | fusion_gateway / tray 自启动 | `PromLight-MCPWatchdog`（**已移除**） |
| codex hooks | `codex_hook.py` 接 5 事件 | `agent_hook.py`（`D:/Promlight_rev/PromLight/`）**仍接 11 事件** |
| 上报端点 | `fusion_gateway:8010` | promlight 自有系统 |

### 3.2 结论：**可以共存，无硬冲突**——但有 3 个潜伏风险

无硬冲突的理由：

1. docker 层只剩 stackchan profile，promlight profile 已删 → **无 profile 碰撞**。
2. MCP 工具命名空间不同（`stackchan_*` vs promlight 工具） → **无工具名碰撞**。
3. 上报端点不同 → **无数据碰撞**。

潜伏风险（必须处理）：

- **风险 A（MEDIUM）**：`~/.codex/hooks.json` 里 stackchan 与 promlight **同时挂载在重叠事件上**（SessionStart/UserPromptSubmit/PermissionRequest/Stop/SessionEnd）。promlight 额外挂了 `PreToolUse`/`PostToolUse`/`SubagentStart`/`SubagentStop`/`PreCompact`/`PostCompact`——这些**在每次工具调用时都触发**。多数 promlight hook 条目**没有 timeout 字段**。若 `agent_hook.py` 卡顿或其目标端点已死，codex 每次工具调用都会被拖慢甚至挂起——这正是"响应卡顿/断流"类体感的潜在放大器。
- **风险 B（LOW）**：SessionEnd 被 codex 强制钳到 3s（用户反馈 #346），而该事件上挂了 2 个 hook，3s 预算被两个脚本瓜分，慢一点就被截断。
- **风险 C（LOW）**：promlight 的 MCP server 和计划任务已删，但 `agent_hook.py` 仍在 hooks.json 里活跃——属于**僵尸钩子**。若它向已不存在的端点发请求且无短超时，会留下错误日志或网络挂起。

### 3.3 共存裁定

**可以共存**。但 promlight 在 codex hooks.json 中的条目必须做超时加固或清理（见 §4 工单 5）。

---

## 第四部分：修复工单（codex 实施）

**审计员下达，codex 负责。每项工单末尾的"验收证据"为必须回传的内容。禁止猜想式实施，所有改动以本部分代码块为准。**

### 工单 1（CRITICAL）：Claude Code hooks 迁移到 settings.local.json

#### 1.1 改 `install_claude_hooks.ps1`

**文件**：`<PROJECT_DIR>\agents\install_claude_hooks.ps1`

**改动点**：target 从 `settings.json` 改为 `settings.local.json`（ccswitch 不动 local 文件，Claude Code 同样读取）。

```powershell
$ErrorActionPreference = "Stop"

# 关键改动: hooks 写入 settings.local.json, 避免被 ccswitch 全量覆盖 settings.json 时抹掉
$settingsPath = Join-Path $env:USERPROFILE ".claude\settings.local.json"

$python = "C:\Users\<USER>\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $python)) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw "找不到 Python" }
}
$hookScript = "<PROJECT_DIR>\agents\claude_hook.py"
$hookCmd = "`"" + ($python -replace '\\', '/') + "`" `"" + ($hookScript -replace '\\', '/') + "`""

# 读取现有 settings.local.json (保留 permissions / enableAllProjectMcpServers 等已有键)
$settings = @{}
if (Test-Path $settingsPath) {
    $raw = Get-Content $settingsPath -Raw -Encoding UTF8
    if ($raw) {
        try { $settings = $raw | ConvertFrom-Json -AsHashtable } catch { $settings = @{} }
    }
}
if (-not $settings.ContainsKey("hooks")) { $settings["hooks"] = @{} }

# 标准 hooks 结构: 事件 -> [ { matcher: "", hooks: [ { timeout, type, command } ] } ]
foreach ($event in @("Stop", "SessionEnd", "Notification", "PermissionRequest")) {
    $list = @()
    if ($settings["hooks"].ContainsKey($event)) { $list = @($settings["hooks"][$event]) }
    $exists = $false
    foreach ($entry in $list) {
        if ($entry.hooks -and @($entry.hooks | Where-Object { $_.command -like "*claude_hook.py*" }).Count -gt 0) {
            $exists = $true; break
        }
    }
    if (-not $exists) {
        $list += @{ matcher = ""; hooks = @(@{ timeout = 30; type = "command"; command = $hookCmd }) }
    }
    $settings["hooks"][$event] = $list
}

$backup = "$settingsPath.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
if (Test-Path $settingsPath) { Copy-Item $settingsPath $backup }
$settings | ConvertTo-Json -Depth 8 | Set-Content $settingsPath -Encoding UTF8
Write-Host "hooks installed -> $settingsPath (backup: $backup)"
```

#### 1.2 立即执行

```powershell
powershell -ExecutionPolicy Bypass -File "<PROJECT_DIR>\agents\install_claude_hooks.ps1"
```

#### 1.3 验收证据（codex 回传）

1. `python -c "import json;d=json.load(open(r'%USERPROFILE%\.claude\settings.local.json',encoding='utf-8'));print('hooks' in d, list(d.get('hooks',{}).keys()))"` 输出含 `Stop/SessionEnd/Notification/PermissionRequest`。
2. 跑一次 `claude --print "审计测试"`，回传 `gateway\state\claude_hook.log` 末 3 行 + `gateway\gateway.log` 中含 `agent_event` 且 status 200 的行。
3. 确认 `~/.claude/settings.json` 是否仍无 hooks（应仍无，hooks 现在在 local 文件）。

---

### 工单 2（CRITICAL）：ccswitch 改为合并写，禁止全量覆盖 settings.json

#### 2.1 先定位 ccswitch 写盘代码

```bash
# ccswitch 代理在 127.0.0.1:15721, 找它的进程与代码位置
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 15721 -State Listen | Select-Object OwningProcess | ForEach-Object { Get-Process -Id $_.OwningProcess } | Select-Object Id,ProcessName,Path"
# 常见位置: ~/.ccswitch/, ~/.claude/ccswitch/, 或 npm 全局包
where.exe ccswitch 2>nul
npm ls -g 2>nul | findstr /i ccswitch
```

#### 2.2 硬性改法（无论 ccswitch 是 node 还是 python）

找到它写 `~/.claude/settings.json` 的函数，**替换为合并写**：

```python
# 严禁这样做 (当前 bug 根因):
# json.dump({"env": new_env, "includeCoAuthoredBy": False}, f)

# 必须这样做:
import json, pathlib
p = pathlib.Path.home() / ".claude" / "settings.json"
data = {}
if p.exists():
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
data["env"] = new_env          # 只更新 env 段
data.setdefault("includeCoAuthoredBy", False)
# hooks / permissions / 其他键原样保留, 严禁删除
p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
```

#### 2.3 若 ccswitch 不可改（闭源/第三方）

则工单 1 的 `settings.local.json` 落点已是强制兜底，**必须保留**。同时在 `install_claude_hooks.ps1` 末尾加自愈说明：

```powershell
# 自愈: hooks 已写入 settings.local.json, ccswitch 覆盖 settings.json 不影响 hooks
Write-Host "提示: hooks 已写入 settings.local.json, ccswitch 覆盖 settings.json 不影响 hooks"
```

#### 2.4 验收证据

1. ccswitch 进程路径 + 写盘代码片段（diff before/after）。
2. 切模型 3 次（deepseek→glm→kimi 循环），每次后 `python -c "import json;print('hooks' in json.load(open(r'%USERPROFILE%\.claude\settings.local.json',encoding='utf-8')))"` 输出 True。
3. 3 次切换后 `claude --print "test"` 仍能触发 `claude_hook.log` 新行。

---

### 工单 3（HIGH）：消除 VS Code 语音派发的静默退化

#### 3.1 `agents_core.py` — `query()` 对 vscode 显式拒发

**文件**：`<PROJECT_DIR>\gateway\agents_core.py`

**问题行**：`query()`（line 378）在 `spawn_visible` 失败时回退 `run_agent`，对 vscode 会执行 `code -r "<task文本>"`（把任务文本当文件打开）。

**改法**：在 `query()` 里对 vscode 显式拦截。

```python
def query(agent: str, task: str, timeout_s: int = 120, visible: bool = True) -> str:
    agent = normalize_agent(agent)
    ok, info = probe(agent)
    if not ok:
        return f"[{agent}] 未在电脑启动，请先打开它 ({info})"
    # === 新增: vscode 不支持语音派发任务, 显式返回, 禁止静默退化成 code -r ===
    if agent == "vscode":
        return ("VS Code 暂不支持语音派发任务。"
                "请在 VS Code 里手动运行任务, 任务结束会经 vscode_hook 自动播报。")
    if visible:
        ok2, msg = spawn_visible(agent, task)
        if ok2:
            return msg
    res = run_agent(agent, task, timeout_s)
    if res["ok"]:
        out = (res["out"] or "").strip()
        text = out[:2000] if out else f"{agent} 执行成功(无输出)"
    else:
        text = f"{agent} 执行失败(rc={res['rc']}): {(res['err'] or res['out'])[:600]}"
    events_append(agent, "done", text[:300])
    return text
```

#### 3.2 可选（实验性）：VS Code 可见执行

若要支持，新增 `agents/vscode_visible_run.py`，靠 VS Code 已打开的前提下用 `code` CLI 的 `--command` 触发集成终端 sendSequence。**仅当 VS Code 已在前台运行时可靠**，否则不可用。codex 评估后若觉得不稳定，**直接采用 3.1 的拒发方案即可**，不要交付半成品。

`VISIBLE_SPECS` 不要加 vscode 项，除非 3.2 实测通过。

#### 3.3 验收证据

1. `agents_core.py` diff。
2. 通过机器人说"让 vscode 总结项目"，确认机器人播报的是 3.1 的拒发文本，**而不是** `code -r` 打开一个文件。回传 `gateway.log` 中该次 agent_query 的返回文本。

---

### 工单 4（HIGH）：Claude Code 流式中断兜底 + VS Code 任务自动上报

#### 4.1 `claude_hook.py` — SessionEnd 强制兜底 done

**文件**：`<PROJECT_DIR>\agents\claude_hook.py`

**问题**：Response stalled mid-stream 时 Stop 可能不触发或 transcript 为空，导致 done 永不上报。

**改法**：在 `__main__` 的 Stop/SessionEnd 分支增加空摘要兜底。

```python
    if hook in ("Stop", "SessionEnd"):
        msg_uid = _msg_uid(data)
        if not _recently_done(msg_uid):
            summary = (_summary_from_transcript(data.get("transcript", []))
                       or _summary_from_transcript_path(data.get("transcript_path") or data.get("transcriptPath") or ""))
            if not summary:
                # === 新增: 流式中断兜底 ===
                # Stop/SessionEnd 触发但无 assistant 文本 -> 响应可能中断(stalled)
                # 仍上报 done, 摘要标注中断, 严禁静默不发
                summary = "Claude 会话结束(响应可能中断, 详见电脑)"
            _post("done", summary, session_id, msg_uid)
```

#### 4.2 `vscode_hook.py` — PowerShell profile 手动上报函数

**文件**：`<PROJECT_DIR>\agents\vscode_hook.py`

当前 `install_tasks` 已存在，补一个 `--install-profile` 把上报命令注入 PowerShell profile（仅对显式 `--install-profile` 时启用，避免污染所有终端）：

```python
def install_profile() -> str:
    """向 PowerShell profile 追加一个函数 Invoke-StackChanReport,
    用户在 VS Code 终端跑完任务后手动调一次即可上报。不自动 hook 每条命令(太吵)。"""
    import os
    profile = os.path.expandvars(r"%USERPROFILE%\Documents\PowerShell\Microsoft.PowerShell_profile.ps1")
    os.makedirs(os.path.dirname(profile), exist_ok=True)
    hook = str(Path(__file__).resolve())
    snippet = (
        "\n# === StackChan VS Code 上报 (手动调用) ===\n"
        "function Invoke-StackChanReport($summary) {\n"
        f'    python "{hook}" --summary $summary\n'
        "}\n"
        "function Invoke-StackChanReportLast {\n"
        '    $s = (Get-History -Count 1).CommandLine\n'
        '    if ($s) { python "' + hook + f'" --summary ("完成: " + $s.Substring(0,[Math]::Min(120,$s.Length))) }}\n'
    )
    existing = Path(profile).read_text(encoding="utf-8") if Path(profile).exists() else ""
    if "Invoke-StackChanReport" not in existing:
        Path(profile).write_text(existing + snippet, encoding="utf-8")
    return profile
```

并在 `main()` 的 args 解析里加 `--install-profile` 分支。

#### 4.3 验收证据

1. `claude_hook.py` diff。
2. 模拟中断测试：`claude --print "写一段长文"` 中途 Ctrl+C，确认 `claude_hook.log` 出现 `posted done` 且摘要含"响应可能中断"。回传该日志行。
3. `vscode_hook.py` diff + `--install-profile` 运行后 profile 文件路径。

---

### 工单 5（MEDIUM）：清理 codex hooks.json 中 promlight 僵尸/无超时条目

#### 5.1 推荐：promlight 不再用 → 全量移除 promlight 条目

**文件**：`C:\Users\<USER>\.codex\hooks.json`

**最终产物**（仅保留 stackchan `codex_hook.py`，promlight 全部移除）：

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "C:/WINDOWS/py.EXE -3 <PROJECT_ROOT>/fusion.firmware.0731/agents/codex_hook.py", "statusMessage": "Notifying StackChan", "timeout": 10 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "C:/WINDOWS/py.EXE -3 <PROJECT_ROOT>/fusion.firmware.0731/agents/codex_hook.py", "statusMessage": "Notifying StackChan", "timeout": 10 } ] }
    ],
    "PermissionRequest": [
      { "hooks": [ { "type": "command", "command": "C:/WINDOWS/py.EXE -3 <PROJECT_ROOT>/fusion.firmware.0731/agents/codex_hook.py", "statusMessage": "Notifying StackChan", "timeout": 10 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "C:/WINDOWS/py.EXE -3 <PROJECT_ROOT>/fusion.firmware.0731/agents/codex_hook.py", "statusMessage": "Notifying StackChan", "timeout": 10 } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "timeout": 3, "type": "command", "command": "C:/WINDOWS/py.EXE -3 <PROJECT_ROOT>/fusion.firmware.0731/agents/codex_hook.py", "statusMessage": "Notifying StackChan" } ] }
    ]
  }
}
```

**注意**：移除前先备份 `cp ~/.codex/hooks.json ~/.codex/hooks.json.bak-audit-$(date +%Y%m%d-%H%M%S)`。codex 自身会重新计算 `hooks.state` 里的 `trusted_hash`，**不要手动改 hooks.state**，让 codex 启动时自动重新信任。

#### 5.2 备选：promlight 仍要用 → 给所有条目补 timeout

若用户确认 promlight 仍用，则保留 promlight 条目，但**每一条都必须加 `timeout`**（PreToolUse/PostToolUse/SubagentStart/SubagentStop/PreCompact/PostCompact 当前缺失）。统一 `"timeout": 5`。同时核查 `D:/Promlight_rev/PromLight/agent_hook.py` 内部所有 `urllib.request.urlopen(...)` 的 timeout ≤ 3，防止僵尸端点挂起 codex 工具调用。

#### 5.3 验收证据

1. `hooks.json` diff（before/after）。
2. codex 重启后无 hook 信任报错（回传 codex 启动日志前 20 行）。
3. 跑一次 `codex exec "echo ok"`，对比修改前后耗时（应不增加）。

---

### 工单 6（LOW，可选）：固件 LED / 打断历史遗留

与本次 Claude Code 耦合审计无关。若要一并处理，严格按 thread 中 antigravity 的"抓包 / addr2line 铁证"原则，禁止猜想式改 bug。**本次不下达具体代码**，待用户另行指示。

---

## 第五部分：实施顺序与总结性约束

### 5.1 实施顺序与依赖

```
工单 1 (hooks 迁 local)  ──┐
                          ├─► 工单 2 (ccswitch 合并写)  ──► 验证不复发
                          │
工单 5 (promlight 清理)   ─┘  (独立, 可并行)

工单 3 (vscode 拒发)      ──► 独立
工单 4 (claude 中断兜底)  ──► 依赖工单 1 完成(hooks 接上后才有意义)
```

**强制顺序**：工单 1 → 工单 4 → 工单 2 → 工单 5 → 工单 3。工单 1 不完成，工单 4 无法验证。

### 5.2 总结性约束（再次强调）

1. **严禁**把 hooks 写回 `~/.claude/settings.json` 顶层——ccswitch 会抹掉。只能写 `settings.local.json`。
2. **严禁**对 vscode 走 `code -r <task文本>` 静默退化——必须显式拒发。
3. **严禁**在 `claude_hook.py` Stop/SessionEnd 无摘要时静默不发——必须兜底 done。
4. **严禁**保留 promlight 在 codex hooks.json 中无 timeout 的 PreToolUse/PostToolUse 条目。
5. 每个工单的"验收证据"必须回传，审计员据证据复核。没有证据的工单视为未完成。

### 5.3 最终裁定

1. **StackChan × Claude Code 耦合：当前处于断联状态**。脚本和网关都写对了，唯一断点在 `~/.claude/settings.json` hooks 段被 ccswitch 抹掉。修好工单 1+2 即可恢复 CLI 与 VS Code 插件的双向耦合。
2. **VS Code（非 Claude 插件）作为独立 agent：结构性残缺**，需工单 3+4 补齐。
3. **StackChan-fusion 与 PromLight：可共存，无硬冲突**。promlight 的 docker profile / MCP server / 计划任务已清理干净，残留仅是 codex hooks.json 里的僵尸/超时缺失条目，按工单 5 处理即可。

**最高优先级**：工单 1（恢复 claude hooks 到 settings.local.json）→ 工单 2（ccswitch 合并写）→ 工单 5（promlight hook 清理）。这三项不修，Claude Code 侧任何"机器人没播报"的投诉都会无限复发。

---

*审计员等 codex 回传工单 1 + 5 的验收证据后复核。*
