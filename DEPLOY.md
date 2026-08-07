# StackChan 融合方案 · 部署指南（新电脑 / 新机器人）

> 本指南用于在一台**全新电脑**上部署，并接入一台**新的 StackChan 机器人**。
> 所有 `YOUR_*` 占位符都必须替换成你自己的值；**不要提交真实 token/密钥到仓库**。

## 0. 前置条件

| 依赖 | 说明 |
|---|---|
| Windows 10/11 | 主控电脑 |
| Docker Desktop 4.62+ | 自建 xiaozhi-esp32-server（备用链路）+ MCP Toolkit |
| Python 3.11+ | 网关 / 桥接 / 验证脚本 |
| Node.js 18+ | codex / claude / pi CLI 的 npm 包装 |
| Tailscale | 内网穿透（AP 隔离网络必须；机器人经 Funnel 443 回连） |
| 各 agent CLI | codex、claude、agy（Antigravity CLI）、pi（pi-coding-agent） |

## 1. 机器人侧

### 1.1 刷固件

固件在 `firmware/post-fw-v1.2-mqttpush/`（M5Stack CoreS3，分区 app @ 0x410000，16MB；
含 µ-law MQTT 主动播报、msg_uid/ACK 闭环、Phase 8 动作/拍照、v08.08 LED 灯环根治）。

```powershell
# 全量（首次 / 换固件必须）：会擦除，需重新配网
python -m esptool --chip esp32s3 --port COMx erase_flash
python -m esptool --chip esp32s3 -b 460800 --port COMx `
  --before default-reset --after hard-reset `
  write-flash --flash-mode dio --flash-size 16MB --flash-freq 80m 0x0 merged-binary.bin

# 升级（已在本固件布局上）：保留配置
python -m esptool --chip esp32s3 -b 460800 --port COMx `
  --before default-reset --after hard-reset write-flash 0x410000 xiaozhi.bin
```

> 其他型号的 StackChan：若分区/板型不同，需用对应固件或重新编译
> （构建: espressif/idf:v5.5.2，板型 M5STACK_CORE_S3，见 firmware/build_led_ci.sh）。

### 1.2 配网

1. 拔 USB + 电池 30 秒再上电，屏幕出现 `XiaoZhi-xxxx` 热点；
2. 连热点 → 打开 `http://192.168.4.1`；
3. 选你的 WiFi，输密码；
4. 服务器设置选**自定义服务器**，填你的 Funnel 域名：
   `https://YOUR_FUNNEL_DOMAIN.ts.net`（固件自动推导 OTA=/xiaozhi/ota/、WS=/xiaozhi/v1/）；
5. 保存重启。

### 1.3 绑定 xiaozhi.me（云链路）

1. [xiaozhi.me](https://xiaozhi.me) 注册/登录，添加设备（记下**设备 ID**，机器人会播报）；
2. 创建智能体（建议命名 STACK），**系统提示词**粘贴 `prompt-阿松-v3.md` 全文；
3. 智能体 → MCP 设置 → **获取接入点**，得到 `wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN`；
4. 把设备绑定到该智能体；唤醒词在固件里（本方案为「阿松」）。

## 2. PC 侧

### 2.1 解包与依赖

```powershell
# 解压迁移包（package-stackchan.zip 或直接从仓库拉取本目录）
pip install mcp uvicorn starlette websockets python-dotenv
# 安装 agent CLI（如已装可跳过）
npm install -g @openai/codex
# claude / pi / agy 按各自官方方式安装
```

### 2.2 配置（全部换成自己的值）

**`xiaozhi-mcp/.env`**（从 `.env.example` 复制）：

```ini
MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN_HERE
```

**`gateway/config.json`**（从 `config.json.example` 复制）：

| 字段 | 填什么 |
|---|---|
| `ota_url` | `https://YOUR_FUNNEL_DOMAIN.ts.net/xiaozhi/ota/` |
| `robot_mac` | 你的机器人 MAC（电脑里 `ipconfig /all` 看不到，用固件配网页或路由器查） |
| `endpoint_health_url` | 备用链路的 health URL + 容器日志里的 key |
| `auth_token` / `push_secret` | 自己生成的长随机串（两处保持一致） |

### 2.3 启动服务

```powershell
# 备用链路容器（可选，云链路为主时可不启）
docker compose -f server/docker-compose.fusion.yml up -d

# 融合网关（必须）
powershell -ExecutionPolicy Bypass -File gateway/run_gateway.ps1

# 云桥接（必须，机器人走 xiaozhi.me 时）
powershell -ExecutionPolicy Bypass -File xiaozhi-mcp/run_bridge.ps1

# 托盘（可选，状态监视 + 网关守护）
powershell -ExecutionPolicy Bypass -File gateway/install_autostart.ps1
```

### 2.4 四 agent 接入（hooks）

| Agent | 配置 |
|---|---|
| codex | `~/.codex/hooks.json` 各事件指向 `agents/codex_hook.py`；`~/.codex/config.toml` 加 `bypass_hook_trust=true`、`[windows] sandbox='unelevated'` |
| claude | `~/.claude/settings.local.json` hooks 指向 `agents/claude_hook.py`（跑 `agents/install_claude_hooks.ps1`；local 优先级高，ccswitch 切模型不覆盖）；确认回环需启动 `agents/confirm_mcp.py` |
| agy / Antigravity | `~/.gemini/config/hooks.json` 的 `stackchan` 段指向 `agents/antigravity_hook.py`（命名空间必须是 `stackchan`/`promlight`，IDE 才识别；改完需重启 Antigravity 桌面版） |
| pi | `~/.pi/agent/settings.json` 注册 `extensions/hooks-bridge.ts`（把文件放进 `~/.pi/agent/extensions/`） |

> 所有 hook 脚本里的路径如 `<PROJECT_ROOT>\...` 需改成你机器上的实际路径。

## 3. 验证

```powershell
python scripts/verify_connectivity.py
```

全部 PASS 后：

1. 对机器人说「阿松」唤醒 → 应自动检查待播报消息（唤醒优先规则）；
2. 「检查 agent 状态」→ 应播报 4 个 agent 状态；
3. 「让 codex 总结一下当前项目」→ 电脑弹出 Codex 窗口执行 → 完成后唤醒机器人 → 播报结果。

## 4. 敏感信息清单（发布前必查）

| 敏感项 | 仓库中的处理 |
|---|---|
| xiaozhi.me MCP token | `.env.example` 用 `YOUR_TOKEN_HERE`；真实值只在本地 `.env` |
| gateway auth_token / push_secret | 代码默认值/示例用 `YOUR_GATEWAY_TOKEN`；真实值只在本地 `config.json` |
| OTA/机器人 MAC | 占位 `YOUR_FUNNEL_DOMAIN.ts.net` / `AA:BB:CC:DD:EE:FF` |
| mcp-endpoint health key | 占位 `YOUR_HEALTH_KEY`（部署时从容器日志获取） |
| DeepSeek/Qwen API key | 只在本地 docker 配置或环境变量，不入库 |
| Tailscale 个人域名 | 一律用 `YOUR_FUNNEL_DOMAIN.ts.net` 占位 |

`.gitignore` 已忽略 `.env`、`config.json`、日志、状态、outbox、打包产物。

