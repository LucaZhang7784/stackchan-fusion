# StackChan 融合方案 — fusion.firmware.0731

> **中文版**: [README.md](README.md) · **English**: [README.en.md](README.en.md)

日付: 2026-08-01
範囲: 「xiaozhi.me クラウドエージェント + Tailscale」と「StackChan ロボットのツール
能力」を統合し、**ロボットとローカル4エージェント（agy / pi / claude / codex）の
双方向音声通信**を実現します。

> 現在のメイン経路（2026-08-01 実測済み）: ロボットは xiaozhi.me クラウド STACK
> エージェントを使用し、xiaozhi-mcp bridge 経由でローカル fusion-gateway に接続、
> **Docker MCP Toolkit** を通じて **Codex / Claude Code / VS Code** に公開。
> 自前の xiaozhi-esp32-server 経路は予備として保持。

## 1. 結論（まずここを読む）

1. **ロボットファームウェア**: M5Stack 公式ファームウェア（アクティベーションコード
   はバインド解除済み）、xiaozhi.me の STACK エージェントにバインド。
2. **ロボット → エージェント**: クラウド LLM は xiaozhi-mcp bridge（wss エンドポイント）
   経由でローカル8ツールを取得 — `agent_status` / `agent_query` / `agent_pending` /
   `agent_confirm` / `claude_query` / `codex_query` / `docker_status` /
   `agent_result_check`。音声コマンドでローカルエージェントを直接駆動。
3. **エージェント → ロボット**: エージェントイベントはゲートウェイでキューイングされ、
   ロボットは起床後に LLM が `agent_pending` を呼んで読み上げ（クラウド経路には
   プッシュチャネルなし; 自前サーバー経路では `robot_say` で即時プッシュ可能）。
4. **デスクトップ統合**: Docker MCP Toolkit（profile=stackchan）が 11 個の
   ゲートウェイツールを Codex / Claude Code / VS Code にクライアント設定ゼロで公開。
5. **接続性検証**: ゲートウェイ `/healthz` + `docker mcp gateway run --dry-run` +
   実機音声テスト（例: 「codex の状態」→ ロボットが「codex 状態正常」と応答）。

## 2. アーキテクチャ

```
                 ┌────────────────────── ローカル (Windows) ───────────────────┐
 ロボット (ESP32)│  Docker: xiaozhi-esp32-server (8000/8003)                    │
 公式FW 2.2.6    │    ├─ SERVER_MCP ──► fusion_gateway.py (8010, Bearer認証)    │
   │ 外向き WSS  │    └─ MCP エンドポイント ─► mcp-endpoint-server (8004)       │
   ▼            │                              ▲                                │
 Funnel 443     │        ┌─────────────────────┴───────────────┐                │
 (Tailscale)    │  Codex / Claude Code  (MCP client -> 8010)    │                │
                │    robot_say / robot_status / codex_query ... │                │
                └────────────────────────────────────────────────┘               │
```

- ゲートウェイは2つのトランスポートに対応: `--http` は xiaozhi SERVER_MCP と
  Claude Code（HTTP MCP）用、`--stdio` は Codex CLI / その他 stdio クライアント用。
- HTTP モードは `Authorization: Bearer <token>` を強制（fail-closed）。
  `/healthz` のみ認証不要。

## 3. ファイル一覧

| パス | 説明 |
|---|---|
| gateway/fusion_gateway.py | 融合ゲートウェイ本体（単一ファイル、フレームワーク依存なし） |
| gateway/config.json | 実設定（OTA/MAC/health key/token/ポート） |
| gateway/agents_core.py | マルチエージェント中核（agy/pi/claude/codex）+ イベント/確認ストレージ |
| gateway/run_gateway.ps1 / stop_gateway.ps1 | ゲートウェイ起動/停止 |
| gateway/watchdog_gateway.ps1 | ゲートウェイ監視（2分毎チェック、落ちたら自動再起動） |
| gateway/fusion_tray.ps1 | タスクトレイ状態ツール（ゲートウェイ/MCP/ロボット 3色） |
| gateway/install_autostart.ps1 | 一括登録: 自動起動 + watchdog + トレイ |
| docker/fusion-gateway.yaml | MCP Toolkit サーバー定義（remote + streamable-http + Bearer） |
| docker/mcp-toolkit-profile.json | profile `stackchan` エクスポート（移行用） |
| docker/host-executor.py | Windows ホスト実行器（コンテナ内ゲートウェイがローカル CLI を呼ぶ用） |
| docker/run_executor.ps1 / install_executor_task.ps1 | 実行器の起動 + 自動起動 |
| docker/MCP-Toolkit接入说明.md | Toolkit 導入/検証の完全ドキュメント |
| gateway/守护与托盘说明.md | 監視とトレイの使い方 |
| server/.mcp_server_settings.json | xiaozhi 向け SERVER_MCP 新設定（streamable-http） |
| server/deploy_server_mcp.ps1 | SERVER_MCP デプロイ（バックアップ→置換→再起動→検証/ロールバック） |
| server/deploy_fusion_push.ps1 | 一括デプロイ: プッシュパッチ + fusion_secret + SERVER_MCP + 再ビルド |
| server-patch/core/*.py | サーバーパッチ（connection レジストリ / http_server /api/push） |
| server-patch/docker-compose.fusion.yml | パッチオーバーレイマウント（メイン compose と併用） |
| server/prompt_patch.md | プロンプトパッチ: 起床後に保留メッセージを確認 |
| scripts/verify_connectivity.py | 層別接続性検証 |
| scripts/stop_legacy_bridge.ps1 | 廃止された旧 bridge.js を停止 |
| tests/test_gateway.py | ゲートウェイ自己テスト（stdio JSON-RPC） |
| firmware/remote_wakeup_v2.md | v2 主動発話の路線分析（A/B/C） |
| package-stackchan.zip | 完全移行パッケージ（ファームウェア + PC 側 + README） |

## 4. デプロイ手順

```powershell
# 1. ゲートウェイ起動
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\gateway\run_gateway.ps1

# 2. ゲートウェイ自己テスト
python <PROJECT_DIR>\tests\test_gateway.py

# 3. xiaozhi server へデプロイ（SERVER_MCP 登録 + プッシュパッチ /api/push、
#    コンテナ自動停止/起動、バックアップとロールバック付き）
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\server\deploy_server_mcp.ps1

# 4. （任意）起床後にロボットがメッセージを自動取得するプロンプトパッチ
#    server/prompt_patch.md に従いコンテナを再起動

# 5. 接続性検証
python <PROJECT_DIR>\scripts\verify_connectivity.py
```

## 5. エージェント側の接続方法

**推奨（現在使用中）**: Docker MCP Toolkit での一括接続 — 第9章参照。
クライアント設定は `docker mcp client connect` が自動で書き込み:

- Codex: `~/.codex/config.toml` → `[mcp_servers.MCP_DOCKER]`
- Claude Code: `~/.claude.json` → `MCP_DOCKER`
- VS Code: `<プロジェクトルート>/.vscode/mcp.json` → `MCP_DOCKER`

**直接接続（代替）**:

- Claude Code:
  ```
  claude mcp add --transport http fusion http://127.0.0.1:8010/mcp
  ```
  （CLI がヘッダーを必要とする場合: headers Authorization: Bearer YOUR_GATEWAY_TOKEN）
- Codex CLI (~/.codex/config.toml):
  ```toml
  [mcp_servers.fusion]
  command = "python"
  args = ["<PROJECT_DIR>/gateway/fusion_gateway.py", "--transport", "stdio"]
  ```
- 注意: Codex は CLI 版（0.146.0、バックグラウンド起動可）に戻しており、
  ストア版の Access denied 問題は解消済み。

## 6. トラブルシューティング

| 症状 | 確認 |
|---|---|
| サーバーログ `服务端MCP客户端已连接，可用工具: []` | ゲートウェイ未起動 / コンテナが 8010 に到達不可 / token 不一致 |
| `unhandled errors in a TaskGroup` | 旧 `type:"ws"` 設定が残っている（本方式で置換済み）; またはゲートウェイ到達不可 |
| ロボットが何も話さない | まず verify_connectivity.py を実行し、手動で起こして会話 |
| 旧 bridge を止めたい | scripts/stop_legacy_bridge.ps1 -Kill（guard が再起動するため guard も停止） |
| Tailscale 再接続後ゲートウェイ到達不可 | コンテナは YOUR_TAILSCALE_IP:8010 に接続、Tailscale IP が変わっていないか確認 |

## 6.5 デプロイ状況（2026-08-01 実測）

| 項目 | 状態 |
|---|---|
| xiaozhi.me クラウド STACK エージェント | ✅ デバイス ID YOUR_DEVICE_ID バインド済み、エージェント STACK |
| xiaozhi-mcp bridge (wss) | ✅ 稼働中、8 ツール、ハートビート正常（60秒毎 Ping） |
| 融合ゲートウェイ (8010, Bearer認証) | ✅ 稼働中、/healthz 200、11 ツール |
| Docker MCP Toolkit | ✅ profile stackchan 正常読込、19 ツール表示 |
| Codex / Claude Code / VS Code クライアント | ✅ 全て connected（MCP_DOCKER） |
| エージェント検出 | ✅ claude 2.1.220 / codex 0.146.0 / agy 1.1.9 / pi 0.80.3 |
| エンドツーエンドツール呼び出し | ✅ agent_query(pi, "1+1") → 2 |
| ロボット音声テスト | ✅ 「codex の状態」→ ロボット「codex 状態正常」 |
| ゲートウェイ監視 + トレイ | ✅ kill 後約6秒で自動再起動; トレイ3色正常 |

デプロイ中に直した3つの問題:
1. .ps1 の中国語文字化け → 全スクリプトを UTF-8 BOM で保存。
2. .config.yaml が ANSI で読まれ破損 → バックアップから復元、デプロイスクリプトを
   .NET UTF-8 読み書きに変更。
3. FastMCP 1.28 の2つの落とし穴: 外側ラッパーは内側の lifespan を伝播させる必要;
   トランスポートセキュリティはデフォルトで非 localhost の Host ヘッダーを拒否（421）
   → allowed_hosts を追加。

その他修正: MCP Toolkit profile の `description` 欠落で UI「Failed to load profiles」
→ description/icon/readme/metadata を補完して正常読み込み。

## 7. 既知の制約

- v1 の agent→ロボットは「キュー + 起床時読み上げ」であり割り込みプッシュではない。
  真の主動発話: firmware/remote_wakeup_v2.md 参照。
- MQTT リモートウェイク（公式経路）はこのネットワーク（AP分離 + Funnel が
  UDP/1883 非対応）では不可、MQTT を公開する場合を除く。
- M5Stack 公式ファームウェアは元々 8 個のデバイスツール（音量/画面/LED/撮影
  self.camera.take_photo 等）を持ち、関数リストに統合済み。

---

## 8. マルチエージェント双方向通話 (v2, 2026-08-01)

ロボットとローカル4エージェントおよび VS Code プラグインの双方向通信:
**agy (Antigravity CLI) / pi (pi-coding-agent) / claude (Claude Code CLI) /
codex (Codex CLI)**。

### アーキテクチャ

```
ロボット（クラウド STACK エージェント / 自前 docker 両対応）
   │ 音声
   ▼
xiaozhi LLM ──ツール呼び出し──► xiaozhi-mcp bridge (クラウド) または 融合ゲートウェイ (自前)
                                  │ agents_core.py（共有イベント/確認ストレージ）
                                  │
        ┌─────────────────────────┼──────────────────────┐
        ▼                         ▼                      ▼
   agent_query                agent_pending         agent_confirm
   (agy/pi/claude/codex       (読み上げイベント+     (音声回答の書戻し)
    ヘッドレス実行, done記録)   確認質問)
                                  ▲
   エージェント側イベント ──► 融合ゲートウェイ POST /api/agent_event（hooks/ラッパー）
   claude 権限確認 ──► agents/confirm_mcp.py (permission-prompt-tool)
```

### ツール一覧（クラウド bridge 8 / ゲートウェイ 11）

| ツール | 説明 |
|---|---|
| agent_status(agent=all) | 4 エージェントの CLI 可用性/実行プロセス/保留確認数/最近イベント |
| agent_query(agent, task) | agy/pi/claude/codex をヘッドレス実行、結果をイベント+outbox へ |
| agent_pending(clear) | ロボット側: 保留イベントと確認質問を読み上げ |
| agent_confirm(agent, answer) | ユーザーの音声回答を待機中のエージェントへ書戻し |
| claude_query / codex_query / docker_status / robot_say / robot_pending | v1 ツール保持 |

### 確認ループ（claude 権限要求 → ロボット → 音声回答 → claude）

- `agents/confirm_mcp.py`: MCP サーバー。claude の `--permission-prompt-tool` として使用
- `agents/claude_run.py`: confirm MCP 付きで `claude -p` を実行するラッパー
- `agents/claude_hook.py` + `install_claude_hooks.ps1`: ~/.claude/settings.json に
  Stop/SessionEnd/Notification hooks を導入。VS Code 内の claude セッションも報告
- 流れ: claude が権限要求 → confirm_mcp が質問を登録 → ゲートウェイがキュー
  （自前ならプッシュ可）→ 起床後ロボットの LLM が agent_pending を読み上げ →
  ユーザーが回答 → LLM が agent_confirm 呼び出し → 回答を reply_file へ書込み →
  confirm_mcp が allow/deny を返す → claude 続行

### 使い方

```powershell
# claude 確認ループ付き
python <PROJECT_DIR>\agents\claude_run.py "タスク説明" "作業ディレクトリ"
# VS Code/ターミナルの claude セッションに hooks 導入
powershell -ExecutionPolicy Bypass -File <PROJECT_DIR>\agents\install_claude_hooks.ps1
```

### クラウド STACK エージェントのキャラクター設定（xiaozhi.me コンソールに貼り付け済み、ウェイクワード「阿松」）

```
私の名前は阿松、デスクトップコンパニオン AI。明るく自然な口調で、
返信は 1-2 文・50 字以内。
ツールルール:
- ユーザーが PC のエージェント状態/誰が使えるかを尋ねる: agent_status を呼ぶ
- ユーザーが agy/pi/claude/codex に作業を頼む: agent_query を呼び、すぐ「実行中」と返す
- ユーザーが「メッセージ/タスク/誰か探してる?」と尋ねる: agent_pending を呼んで読み上げ
- ユーザーがエージェントの確認質問に答える: agent_confirm(agent, 回答) を呼ぶ
- ユーザーが「結果出た?」と尋ねる: agent_result_check または agent_pending を呼ぶ
- 簡単なQA/雑談/デバイス操作(うなずき/ライト/撮影)は直接回答かデバイスツールを使用
[LED リングフィードバックルール]
- ユーザーが話している/聞いている時: self.led.set_color, r=0, g=120, b=255（青）
- 返信を読み上げる前: self.led.set_color, r=0, g=255, b=90（緑）
- 読み上げ後: self.led.auto（待機時の暖色オレンジ）
- ユーザーが具体的な色を指定したら: その色に設定
- 重要: 各段階で1回だけ呼ぶこと; LED ツール失敗時は無視
```

> 注: 現在のファームウェアは esp32 ベースの改造版（ウェイクワード「阿松」+ LED
> 状態ライト）。書き込みファイルは `firmware/`（merged-binary.bin + xiaozhi.bin）。

### 既知の制約

- クラウド経路にはプッシュチャネルなし: エージェントイベントはゲートウェイでキュー、
  ロボットは**起床後**に agent_pending で読み上げ（割り込み不可）; 自前経路は robot_say でプッシュ可。
- 確認ループは claude を完全サポート（permission-prompt-tool）; agy/pi はヘッドレス
  クエリ可、対話確認は CLI 対応待ち; codex CLI クエリ可、確認機構は公式対応待ち。
- pi は `--no-context-files` 必須、workdir=ユーザーホーム（一部ディレクトリで
  "content is not iterable" エラー）。
- VS Code プラグイン: claude 拡張は hooks で報告; pi 拡張は hooks 未対応。

---

## 9. Docker MCP Toolkit 一括接続 (2026-08-01)

Docker Desktop 内蔵の **MCP Toolkit**（`docker mcp` CLI）を統合 MCP ゲートウェイとして
使用し、ローカル fusion-gateway（localhost:8010、11 ツール）を
**Codex / Claude Code / VS Code** に公開。クライアントは `MCP_DOCKER` 1つを接続するだけ。

### 9.1 構成

```
Codex Desktop / Claude Code CLI / VS Code MCP
        │  stdio: docker mcp gateway run --profile stackchan
        ▼
Docker MCP Gateway (Toolkit, profile=stackchan)
        │  streamable-http: http://localhost:8010/mcp + Bearer
        ▼
fusion_gateway.py (Windows, :8010, 11 ツール)
        │  agent_query / agent_status / agent_pending / agent_confirm ...
        ▼
agents_core.py → agy / pi / claude / codex CLI
```

### 9.2 主要ファイル

| パス | 説明 |
|---|---|
| pc/docker/fusion-gateway.yaml | MCP Toolkit サーバー定義（remote + streamable-http + Bearer） |
| pc/docker/mcp-toolkit-profile.json | profile `stackchan` エクスポート（移行用） |
| pc/docker/MCP-Toolkit接入说明.md | 完全な導入/検証手順 |
| pc/gateway/watchdog_gateway.ps1 | ゲートウェイ監視（2分毎、自動再起動） |
| pc/gateway/fusion_tray.ps1 | タスクトレイ状態ツール（ゲートウェイ/MCP/ロボット 3色） |
| pc/gateway/守护与托盘说明.md | 監視とトレイの使い方 |

### 9.3 環境要件

- Docker Desktop 4.62+（4.84.0 で実測）
- ユーザー環境変数: `DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS=1`
  （Toolkit がローカル fusion-gateway へ http 接続できるようにする; Toolkit は
  デフォルトで https を強制）
- fusion-gateway が稼働していること: `pc/gateway/run_gateway.ps1`

### 9.4 別PCへの移行

```powershell
# 1) 依存関係インストール
pip install mcp uvicorn starlette websockets python-dotenv

# 2) pc/docker/fusion-gateway.yaml を新PCへコピー
Copy-Item .\pc\docker\fusion-gateway.yaml $HOME\.docker\mcp\catalogs\

# 3) profile をインポート（endpoint/Bearer 設定含む）
docker mcp profile import .\pc\docker\mcp-toolkit-profile.json

# 4) 環境変数を設定しクライアントを接続
[Environment]::SetEnvironmentVariable('DOCKER_MCP_ALLOW_INSECURE_REMOTE_URLS','1','User')
docker mcp client connect codex --global --profile stackchan
docker mcp client connect claude-code --global --profile stackchan
# (VS Code はプロジェクトルートで) docker mcp client connect vscode --profile stackchan

# 5) ゲートウェイ + 監視 + トレイを起動
powershell -NoProfile -ExecutionPolicy Bypass -File .\pc\gateway\install_autostart.ps1
```

> 注意: profile には Bearer token（gateway/config.json の auth_token と一致）が
> 含まれる。移行後は両者を一致させること。新PCの xiaozhi-mcp/.env には自分の
> MCP_ENDPOINT を記入。

### 9.5 監視とトレイ（本機で有効化済み）

2つのスケジュールタスク（install_autostart.ps1 で一括登録、全て
`-WindowStyle Hidden` で静かに実行）:

| タスク | トリガー | 内容 |
|---|---|---|
| StackChan-FusionGateway | ログオン時 | ゲートウェイ起動（サイレント） |
| StackChan-FusionTray | ログオン時 | タスクトレイ状態ツール |

トレイは5秒毎にポーリング: ゲートウェイ /healthz、MCP profile、ロボット bridge の
ハートビート。アイコン: 緑=全て正常 / オレンジ=一部異常 / 赤=ゲートウェイ停止。
状態変化でバルーン通知、右クリックで詳細表示・再起動・トレイ終了。
詳細: gateway/守护与托盘说明.md。

**監視ロジックはトレイに内蔵**: ゲートウェイ停止を検出すると自動で静かに再起動
（30秒デバウンス）。定期タスク不要のため、PowerShell ウィンドウが定期的に
ポップアップすることはありません。詳細: gateway/守护与托盘说明.md。

### 9.6 バックアップとパッケージ

`package_stackchan.py` で `package-stackchan/` + `package-stackchan.zip` を生成
（ファームウェア 7 bin + PC 側 gateway/xiaozhi-mcp/agents/docker + README）、
秘密情報はプレースホルダーに自動置換。移行は zip で行う（9.4 参照）。

---

## 10. 機密情報の置換（公開前に必読）

このリポジトリは**秘匿化済みリリース**です: すべての token / キー /
アクティベーションコード / デバイス識別子 / 内部アドレスはプレースホルダーに
置換されています。デプロイ前に実際の値に置き換えてください。**実際の秘密情報を
GitHub にコミットしないでください。**

| プレースホルダー | 意味 | 場所 |
|---|---|---|
| `YOUR_GATEWAY_TOKEN` | 融合ゲートウェイ Bearer 認証 token | gateway/config.json.example, docker/fusion-gateway.yaml, docker/mcp-toolkit-profile.json |
| `YOUR_HEALTH_KEY` | xiaozhi MCP エンドポイント health key | gateway/config.json.example |
| `YOUR_FUNNEL_DOMAIN.ts.net` | Tailscale Funnel ドメイン | gateway/config.json.example |
| `YOUR_TAILSCALE_IP` | Tailscale 内部 IP | server/.mcp_server_settings.json |
| `AA:BB:CC:DD:EE:FF` | ロボットの MAC アドレス | gateway/config.json.example |
| `YOUR_DEVICE_ID` | xiaozhi.me デバイス ID | README デプロイ状況表 |
| `YOUR_TOKEN_HERE` | xiaozhi.me MCP エンドポイント JWT | xiaozhi-mcp/.env.example |
| `<PROJECT_DIR>` | ローカル絶対パス | 各 .ps1 / mcp_config.json |
| `<USER_HOME>` | ローカルユーザーホーム | 一部スクリプト |

### 置換手順

1. `gateway/config.json.example` を `gateway/config.json` にコピーし、
   `ota_url`（自分の Funnel ドメイン）、`robot_mac`、`endpoint_health_url` の key、
   `auth_token`（自分で決めた強ランダム文字列）を記入。
2. `xiaozhi-mcp/.env.example` を `xiaozhi-mcp/.env` にコピーし、
   xiaozhi.me コンソールの MCP エンドポイント（JWT 含む）を記入。
3. `docker/fusion-gateway.yaml` と `docker/mcp-toolkit-profile.json` の Bearer を
   config.json の `auth_token` と一致させる。
4. 第 9.4 節に従い MCP Toolkit 移行を実行。

### 公開前チェックリスト

- [ ] `YOUR_` プレースホルダーを全検索し、すべて実値に置換済み
- [ ] `.env` / `config.json` / `*.log` を git に追加しない（.gitignore 参照）
- [ ] プッシュ前に `git diff --cached` で秘密情報の混入がないか確認

---

## 11. Acknowledgements（謝辞）

このプロジェクトは以下のオープンソースプロジェクトとサービスに支えられています。
作者の皆様に感謝します:

| プロジェクト | 作者 | 用途 |
|---|---|---|
| [Stackchan-HtSz](https://github.com/mo-hantang/Stackchan-HtSz) | [mo-hantang](https://github.com/mo-hantang) | ローカル HtSz ファームウェアのベース（カスタムスタック/サーボ制御） |
| [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | [xinnan-tech](https://github.com/xinnan-tech) | xiaozhi プロトコルサーバー（SERVER_MCP / プッシュパッチの基盤） |
| [StackChan](https://github.com/hylarucoder/StackChan) | [hylarucoder](https://github.com/hylarucoder) | StackChan ファームウェア改造の参考（サーボ/カメラ/ウェイクワード） |
| [stackchan-xiaozhi-firmware](https://github.com/heavenchenggong/stackchan-xiaozhi-firmware) | [heavenchenggong](https://github.com/heavenchenggong) | ローカルファームウェアのベース版（ウェイクワード + Servo MCP + 常時稼働） |
| [stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge) | [heavenchenggong](https://github.com/heavenchenggong) | ロボット ↔ Claude Code ブリッジのアーキテクチャ参考 |
| [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) | [migratorywhale](https://github.com/migratorywhale) | ロボット MCP ツール能力の研究 |
| [mcp-calculator](https://github.com/78/mcp-calculator) | [78](https://github.com/78) | MCP Server サンプル（xiaozhi.me エンドポイント接続） |
| [xiaozhi.me](https://xiaozhi.me) | xiaozhi チーム | クラウドエージェントプラットフォーム / MCP エンドポイント |

上記プロジェクトの作者とコミュニティに改めて感謝します。

> 免責事項: 本リポジトリは個人の実験プロジェクトであり、謝辞に記載した
> 各作者とは一切関係ありません。

---

## 12. キーワード Keywords 关键词

**日本語**: スタックチャン · デスクトップロボット · 音声アシスタント · LLM · MCP · エージェント · ESP32 · M5Stack · 小智 · Codex · Claude Code · IoT · ロボット

**English**: stackchan · m5stack · esp32 · xiaozhi · mcp · model-context-protocol · ai-agent · claude-code · codex · voice-assistant · iot · robot · llm

**中文**: 桌面机器人 · 语音助手 · 大模型 · MCP · 智能体 · 双向通话 · ESP32 · M5Stack · 小智 · Codex · Claude Code · 物联网 · 树莓派(可选)
