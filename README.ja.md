# StackChan Fusion — 音声で動かすデスクトップロボット × ローカルAIエージェント

> **English**: [README.en.md](README.en.md) · **中文版**: [README.md](README.md)

**StackChan**（M5Stack CoreS3 デスクトップロボット）を、ローカルのAIエージェント
（**Antigravity (agy) / pi-coding-agent / Claude Code / Codex**）と双方向に
会話できる音声コンパニオンにします。

## できること

- **ロボット → エージェント**: ロボットに話しかけると、クラウドLLM（xiaozhi.me 経由）が
  ローカルツール（`agent_query` / `agent_status` / `agent_pending` / `agent_confirm`）を呼び、
  ローカルエージェントCLIを実行して結果を読み上げます。
- **エージェント → ロボット**: エージェントのイベントはキューに保存され、ロボットは
  起床後に `agent_pending` で読み上げます（自前サーバー経由なら `robot_say` で即時プッシュも可能）。
- **デスクトップ統合**: Docker MCP Toolkit が11個のゲートウェイツールを
  Codex / Claude Code / VS Code へ `MCP_DOCKER` 一本で公開します。

## アーキテクチャ

```
StackChan ロボット (ESP32, カスタムファームウェア, ウェイクワード「阿松」)
        │  音声 (xiaozhi.me クラウド STACK エージェント)
        ▼
xiaozhi.me MCP エンドポイント (wss) ──► xiaozhi-mcp bridge (8 ツール)
                                          │ agents_core.py
                                          ▼
                                fusion-gateway.py (:8010, 11 ツール)
                                          │
        ┌─────────────────────────────────┼──────────────────────┐
        ▼                                 ▼                      ▼
   agent_query                      agent_pending          agent_confirm
   (agy/pi/claude/codex)            (保留イベント)         (音声回答の書戻し)
        ▲
        └── エージェントフック ──► POST /api/agent_event

Codex / Claude Code / VS Code ──► Docker MCP Toolkit (profile: stackchan)
                                      └──► fusion-gateway (:8010/mcp)
```

## コンポーネント

| パス | 役割 |
|---|---|
| `gateway/fusion_gateway.py` | 融合ゲートウェイ (FastMCP, HTTP + stdio), 11 ツール |
| `gateway/agents_core.py` | マルチエージェント中核: status/query/pending/confirm |
| `gateway/watchdog_gateway.ps1` | ゲートウェイ監視 (クラッシュ時自動再起動) |
| `gateway/fusion_tray.ps1` | タスクトレイ監視 (ゲートウェイ/MCP/ロボット状態) |
| `docker/fusion-gateway.yaml` | MCP Toolkit サーバー定義 |
| `docker/mcp-toolkit-profile.json` | MCP Toolkit プロファイル (`stackchan`) のエクスポート |
| `docker/host-executor.py` | Windows ホスト実行器 (コンテナ内ゲートウェイ用) |
| `xiaozhi-mcp/server.py` + `mcp_pipe.py` | xiaozhi.me MCP エンドポイントへのクラウドブリッジ |
| `agents/` | Claude hooks, confirm MCP, claude_run ラッパー |
| `firmware/post-fw-v1.0.0-led/` | カスタムファームウェア (ウェイクワード + LED 状態表示) |

## ツール (ゲートウェイ 11 / クラウドブリッジ 8)

| ツール | 説明 |
|---|---|
| `agent_status(agent)` | CLI 可用性・実行プロセス・保留確認数 |
| `agent_query(agent, task)` | agy/pi/claude/codex をヘッドレス実行し結果イベントを記録 |
| `agent_pending(clear)` | 保留中のエージェントイベントと確認質問を読む |
| `agent_confirm(agent, answer)` | ユーザーの音声回答をエージェントへ書戻す |
| `claude_query` / `codex_query` | 単一エージェント直接実行 (v1 ツール) |
| `robot_say` / `robot_pending` | ロボット発話のキュー / 読み上げ |
| `docker_status` / `ws_probe` / `robot_status` | 診断 |

## クイックスタート

```powershell
# 1. 依存関係をインストール
pip install mcp uvicorn starlette websockets python-dotenv

# 2. 設定 (「機密情報の置換」セクション参照)
Copy-Item .\gateway\config.json.example .\gateway\config.json
Copy-Item .\xiaozhi-mcp\.env.example .\xiaozhi-mcp\.env

# 3. ゲートウェイとトレイを起動 (自動起動タスクも登録)
powershell -NoProfile -ExecutionPolicy Bypass -File .\gateway\run_gateway.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\gateway\install_autostart.ps1

# 4. MCP Toolkit クライアントを接続
docker mcp profile import .\docker\mcp-toolkit-profile.json
docker mcp client connect codex --global --profile stackchan
docker mcp client connect claude-code --global --profile stackchan
```

> Windows 専用です。Docker Desktop 4.62+（MCP Toolkit）と、ユーザー環境変数
> `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1`（ローカルゲートウェイへの http 接続を許可）が必要です。

## 機密情報の置換（デプロイ前の必須作業）

このリポジトリは**秘匿化済みリリース**です。すべてのトークン・キー・デバイスID・
内部アドレスはプレースホルダーに置換されています。実際の値に置き換えてください:

| プレースホルダー | 意味 | 場所 |
|---|---|---|
| `YOUR_GATEWAY_TOKEN` | ゲートウェイ Bearer トークン | `gateway/config.json.example`, `docker/*.yaml|json` |
| `YOUR_HEALTH_KEY` | MCP エンドポイント health キー | `gateway/config.json.example` |
| `YOUR_FUNNEL_DOMAIN.ts.net` | Tailscale Funnel ドメイン | `gateway/config.json.example` |
| `YOUR_TAILSCALE_IP` | Tailscale 内部 IP | `server/.mcp_server_settings.json` |
| `AA:BB:CC:DD:EE:FF` | ロボットの MAC アドレス | `gateway/config.json.example` |
| `YOUR_DEVICE_ID` | xiaozhi.me デバイス ID | README ステータス表 |
| `YOUR_TOKEN_HERE` | xiaozhi.me MCP JWT | `xiaozhi-mcp/.env.example` |
| `<PROJECT_DIR>` / `<USER_HOME>` | ローカル絶対パス | スクリプト / 設定 |

`.env`・`config.json`・`*.log` は絶対にコミットしないでください。

## Acknowledgements（謝辞）

- [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) by [xinnan-tech](https://github.com/xinnan-tech)
- [StackChan](https://github.com/hylarucoder/StackChan) by [hylarucoder](https://github.com/hylarucoder)
- [stackchan-xiaozhi-firmware](https://github.com/heavenchenggong/stackchan-xiaozhi-firmware) by [heavenchenggong](https://github.com/heavenchenggong)
- [stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge) by [heavenchenggong](https://github.com/heavenchenggong)
- [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) by [migratorywhale](https://github.com/migratorywhale)
- [mcp-calculator](https://github.com/78/mcp-calculator) by [78](https://github.com/78)
- [xiaozhi.me](https://xiaozhi.me) — クラウドエージェントプラットフォーム / MCP エンドポイント

## キーワード

`stackchan` · `m5stack` · `esp32` · `xiaozhi` · `mcp` · `model-context-protocol`
· `ai-agent` · `claude-code` · `codex` · `voice-assistant` · `iot` · `robot`

## ライセンスと免責事項

個人の実験プロジェクトです。謝辞に記載した各作者とは一切関係ありません。
