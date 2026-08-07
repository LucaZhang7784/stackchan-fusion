# StackChan 融合ソリューション

> **v08.08（2026-08-07）:** LED リング根本修正（PY32 GPIO13 初期化 + 待機ロック + I2C ミューテックス；
> 再生=緑 → 待機=暖オレンジを実機確認）、Antigravity 読み上げ修正（hooks 名前空間 `stackchan` +
> AfterAgent/agent.stop エイリアス）、Prompt v3.6（LED はファームウェア管理）、
> ファームウェア v1.2-mqttpush 再ビルド。
> **v08.07（2026-08-07）:** Phase 8 モーション（done→うなずき / question→傾き）+ `robot_snap`
> カメラ MCP（12 ツール）+ Claude hooks を `settings.local.json` へ移行 + VS Code 音声派発を拒否。
> **v08.06（2026-08-06）:** 読み上げパイプラインを刷新 — EMQX MQTT 上の µ-law 配信、
> msg_uid 冪等 + ファームウェア ACK クローズドループ、単一 Worker FIFO プッシュ、
> エージェント別名 + Fail-Fast プローブ、VS Code 連携、対話型 Claude の権限通知。
> 最新状態は `README.md`（中文）が正。

**StackChan デスクトップロボット**（M5Stack CoreS3）から音声で、この PC 上の
**codex / claude / agy / pi** の 4 つの AI エージェントを操作：
状態確認・タスク実行・結果の読み上げ・音声での確認応答。

> コア方針（2026-08-03）：**クラウドリンク + 起床時アナウンス**。ロボットは
> xiaozhi.me クラウドエージェントを利用し、起床のたびに自動で保留メッセージを
> 確認して 1 件ずつ読み上げます。ロボットから発行したタスクは **エージェント専用の
> 可視ウィンドウ**で実行され、結果は hooks 経由でロボットに戻ります。
> 自前の xiaozhi-esp32-server リンクは予備として維持します。

## アーキテクチャ

```
ロボット (M5Stack CoreS3, ファームウェア v1.0.2-micfix, ウェイクワード「阿松」)
   │ 音声 (ASR/LLM/TTS は xiaozhi.me クラウド)
   ▼
xiaozhi.me クラウドエージェント (STACK, プロンプトは prompt-阿松-v3.md)
   │ MCP (wss://api.xiaozhi.me/mcp)
   ▼
xiaozhi-mcp クラウドブリッジ (mcp_pipe.py + server.py, この PC)
   │ agent_status / agent_query / agent_pending / agent_confirm / agent_result_check ...
   ▼
融合ゲートウェイ fusion_gateway.py (:8010, Bearer 認証)
   │
   ├── codex   (hooks: タスク開始/完了/承認要求 → ロボット)
   ├── claude  (hooks + confirm_mcp 確認ループ)
   ├── agy     (Antigravity fusion hooks, CLI は agent=agy として報告)
   └── pi      (拡張 hooks-bridge.ts)
        └── ロボットのタスク → エージェント専用の可視ウィンドウで実行
```

2 つのリンク：

| リンク | 説明 |
|---|---|
| クラウドリンク（メイン） | 音声は xiaozhi.me。エージェントイベントはキューされ、起床後に読み上げ |
| 自前リンク（予備） | ローカル docker xiaozhi-esp32-server + Tailscale Funnel。`robot_say` による真プッシュ対応 |

## 機能

| 機能 | 説明 |
|---|---|
| 起床時アナウンス | 起床のたびにまず `agent_pending` を確認し、メッセージを 1 件ずつ読み上げてから clear |
| 状態確認 | 「XX の状態を確認」→ `agent_status`（4 エージェントの可用性/プロセス/直近イベント、<5秒） |
| タスク実行 | 「XX に〜をさせて」→ `agent_query`。エージェント専用の可視ウィンドウで実行し、結果を返す |
| 確認ループ | claude の権限要求 → ロボットが読み上げ → 音声回答 → allow/deny として回書き（claude は完全対応） |
| デバイス操作 | うなずき/首振り/向き/表情/撮影/LED（ステータス LED はファームウェアが自動追従） |

## クイックスタート

### 新しい PC / 新しいロボット

完全なデプロイ手順（プレースホルダー設定・ファームウェア書き込み・WiFi・
xiaozhi.me バインド・4 エージェントの hooks）は **[DEPLOY.md](DEPLOY.md)** を参照。

### ローカルサービス

```powershell
# 融合ゲートウェイ (:8010, 必須)
powershell -ExecutionPolicy Bypass -File gateway\run_gateway.ps1
# クラウドブリッジ (ロボットが xiaozhi.me 利用時は必須)
powershell -ExecutionPolicy Bypass -File xiaozhi-mcp\run_bridge.ps1
# 予備リンクのコンテナ (任意)
docker compose -f server\docker-compose.fusion.yml up -d
# トレイ + 自動起動 (任意)
powershell -ExecutionPolicy Bypass -File gateway\install_autostart.ps1
```

### 検証

```powershell
python scripts\verify_connectivity.py
```

すべて PASS 後：「阿松」で起床 → 保留メッセージを自動読み上げ。「エージェントの
状態を確認」→ 4 エージェントを報告。「codex にプロジェクトを要約させて」→ Codex
ウィンドウが開いて実行 → 起床後にロボットが結果を読み上げます。

## エージェント連携

| エージェント | 連携方法 | 能動的報告 | 音声での回答書き戻し |
|---|---|---|---|
| codex | `~/.codex/hooks.json` → `agents/codex_hook.py`；`config.toml` `bypass_hook_trust=true`、`[windows] sandbox='unelevated'` | ✅ デスクトップ+CLI | ❌（codex UI で確認） |
| claude | `~/.claude/settings.local.json` hooks → `agents/claude_hook.py`（local ファイルは ccswitch のモデル切替で上書きされない）；可視ウィンドウは `agents/claude_visible_run.py` 経由で完了を報告；`agents/confirm_mcp.py` | ✅ | ✅ 完全ループ |
| agy / Antigravity | `~/.gemini/config/hooks.json` `fusion` ブロック → `agents/antigravity_hook.py` | ✅ CLI は agent=agy | ❌ |
| pi | `~/.pi/agent/extensions/hooks-bridge.ts` → ゲートウェイ | ✅ | ❌ |
| vscode | `agents/vscode_hook.py` がタスク/ターミナル終了時に done を報告；音声派遣は**拒否**（`code -r <task>` でテキストをファイルとして開く事故を防止） | ✅ | ❌ |

タスクはエージェント専用の可視コンソールウィンドウ（タイトル `Codex-Asong` /
`ClaudeCode-Asong` / `Antigravity-Asong` / `pi-Asong`、スクリプトは
`gateway/state/visible_runs/`）で実行。結果は各エージェントの hooks で
ゲートウェイに書き込まれ、起床後にロボットが読み上げます。

## ロボットファームウェア

- 現行：**v1.0.6-ttsbuf**（`firmware/post-fw-v1.0.6-ttsbuf/`）
- ベース：検証済みの 07.31 `reference/stackchan-xiaozhi-firmware`
  （heavenchenggong 系。「阿松」+ LED パッチ込み。**HtSz メインブランチは使用禁止**——
  起動しないバグあり）
- v1.0.6：TTS 再生バッファ拡大（デコードキュー 2.4s→4.8s、再生余裕 2→4、
  キュー満杯時は破棄せず背圧）——長文読み上げの欠落対策
- v1.0.5：マイクゲイン 42→36（クリップ歪み低減）
- v1.0.4：**デバイス側 AEC を撤回**（v1.0.3 の AEC は CoreS3 で audio_input が
  無限ループ、ロボット無反応）
- 継続中の最適化：ウェイク高速化（ウィンドウ 3000→1500ms、しきい値下限 0.35→0.30）；
  予熱 WebSocket 接続（切断後 2 秒で再接続、起床時の再ハンドシェイク不要）
- 前版 v1.0.2-micfix：マイク入力ゲイン 30→42（音声認識の改善）；ウェイクワード「阿松」；
  post-fw レイアウト（app @ 0x410000、16MB）
- アップグレード：`xiaozhi.bin @ 0x410000` を app-only 書き込み（設定保持、
  `firmware/post-fw-v1.0.3-aec-wake/flash_post_fw.ps1`）
- ビルド：espressif/idf:v5.5.2（5.5.4 は黒画面）、手順は `firmware/build_fw_v103.ps1` +
  `build_led_ci.sh`

## サービスと運用

| サービス | ポート | 説明 |
|---|---|---|
| 融合ゲートウェイ | 8010 | 12 個の MCP ツール（`robot_snap` カメラ含む）、Bearer 認証 |
| xiaozhi-mcp クラウドブリッジ | — | mcp_pipe.py + server.py、60 秒ハートビート |
| xiaozhi-esp32-server (Docker) | 8000/8003 | 予備リンク |
| mcp-endpoint-server (Docker) | 8004 | 予備リンクの MCP エンドポイント |
| funnel_proxy.py | 8090 | 予備ルート（自動起動 + 5分自己修復） |
| システムトレイ | — | 状態監視 + ゲートウェイ監視（単一インスタンス保護）+ キュー操作メニュー（表示/クリア） |

監視とスケジュールタスクはすべて `wscript.exe` + VBS の非表示ランチャーで起動
（ウィンドウ点滅なし）。`install_autostart.ps1` で一括登録。

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| 「エージェントの状態を確認」がタイムアウト | ゲートウェイ/ブリッジ停止。プローブはキャッシュ 120 秒 + 並列化（<5秒） |
| codex ウィンドウが Access denied | `~/.codex/config.toml` `[windows] sandbox='unelevated'`。`--sandbox workspace-write` を付けない |
| 中国語タスクが文字化け | hooks は UTF-8 で読み取り。`mcp_pipe` 子プロセス `PYTHONUTF8=1`（修正済み。codex デスクトップ再起動で適用） |
| ロボットが古い結果を読み上げる | `agent_result_check` は 30 分以内の結果のみ返す（修正済み） |
| トレイが 2 つ表示 | `fusion_tray.ps1` 単一インスタンス保護（修正済み） |
| ロボットが読み上げない | 起床しているか、クラウドプロンプトが v3（`prompt-阿松-v3.md`）か確認 |

## 既知の制約

- クラウドリンクは**割り込みプッシュ不可**：エージェントイベントはキューされ、
  起床後に `agent_pending` で読み上げ。真プッシュは自前リンクの `robot_say` のみ。
- Codex / Antigravity デスクトップアプリや VS Code 拡張パネルの内部セッションには
  **外部からタスクを注入できません**。タスクは CLI ウィンドウで実行され、
  プラグインセッションは hooks 経由でイベントを報告します。
- 確認ループが完全なのは claude のみ（`--permission-prompt-tool` + `confirm_mcp`）。
  codex/agy/pi は「承認が必要」を報告するだけで、エージェント UI での確認が必要。
- 音声の往復遅延は約 1.5–2.5 秒（クラウド ASR/LLM/TTS のため）。
  非割り込みアナウンスとしては許容範囲。

## バージョン履歴

### v08.08（2026-08-07 午後）

- **Antigravity 読み上げ修正**: `~/.gemini/config/hooks.json` の名前空間 `"fusion"`→`"stackchan"`
  （IDE は stackchan/promlight のみ認識）；`antigravity_hook.py` のイベント別名に
  AfterAgent/agent.stop/SessionEnd/agent.session.end を追加；13:29 実機 push ack 確認。
- **LED リング根本修正**: PY32 GPIO13 が未初期化（出力+プッシュプル）だったため
  レジスタ書き込みは成功してもリングが点灯しない根因を解消；工場出荷時の初期化シーケンス、
  書込リトライ、read-modify-write、`led_manual_` 待機専用ロック、I2C ミューテックス +
  タッチ冷却を追加；ファームウェア再ビルド（app-only @0x410000）。再生=緑 → 待機=暖オレンジ ✅。
- **Prompt v3.6**: LED 色はファームウェアが管理。LLM は感情表現に `self.led.*` を呼ばない；
  ユーザーが色変更を要求したら同じターンで `self.led.auto` に復帰。
- アーカイブ `version.08.08/` + GitHub 脱敏同期（MAC/ドメイン/キー/ユーザーパス → プレースホルダー）。

### v08.07（2026-08-07）

- **Phase 8.1 モーション連動**：ファームウェアが `done/error` → サーボ Nod、
  `question` → 頭を +15° 傾ける（TiltAsk）；待機中の首振りを 4s → **20s** に
  （`kIdleScanIntervalUs` に統一、実機確認済み）。
- **Phase 8.2 CoreS3 ビジョン MCP**：ゲートウェイに `robot_snap` 追加（12 ツール）；
  ファームウェアが JPEG 撮影 → MQTT チャンク送信（`stackchan/{mac}/photo`, QoS1）→
  ゲートウェイが JPEG マジック + 総長を検証して再構成；実機 3/3 成功。
- **Claude hooks の上書き対策**：`install_claude_hooks.ps1` は
  `~/.claude/settings.local.json` に書き込む（優先度が高く ccswitch で消えない）；
  4 フック注入 + 自己修復ヒント。
- **VS Code 音声派遣を拒否**：`agents_core.query()` が `vscode`/`code` に対し
  音声派遣を拒否し、`code -r <task>` による誤ファイル展開を防止；
  手動タスクは `vscode_hook.py` 経由で自動報告。
- **Claude ストリーム中断フォールバック**：要約が空の場合も
  "Claude 会話終了（応答が中断した可能性、詳細はPCで）" を報告し、イベントを落とさない。
- **codex hooks の整理**：`~/.codex/hooks.json` から PromLight ゾンビ 15 件を削除
  （バックアップ `hooks.json.bak-20260807-110621`）；codex_hook の 5 イベントのみ残す。
- **脱敏**：公開コピーから Tailscale IP `<TAILSCALE_IP>` と実ローカルパスを削除。

### v08.06（2026-08-04）

- ファームウェア v1.0.6-ttsbuf（書き込み済み）：TTS 再生バッファ 2.4s→4.8s、
  再生余裕 2→4、キュー満杯時は破棄せず背圧
- ファームウェア v1.0.5：マイクゲイン 42→36
- **デバイス側 AEC 撤回**：v1.0.3 の AEC が CoreS3 で audio_input を無限ループ
  （task_wdt、無反応/再起動）。addr2line で dios_ssp AEC DSP と特定、VAD(WebRTC)
  パイプラインに復帰。ウェイク高速化と予熱接続（切断後 2 秒再接続）は維持
- Prompt v3 確定（ユーザー承認）：返答言語は xiaozhi.me のプリセットに従う；
  ASR 誤認識時は「意味」で意図判定（読み上げ/状態確認を曲検索と誤解しない）；
  聞き取れない時は「もう一度言って」
- クラウド：STACK エージェントのモデル `deepseek-v4-flash-ha` が
  `503 No available channel` → `qwen3.6` に変更
- **Pending**：長文読み上げが依然として途切れる/欠落——v1.0.6 のバッファ+背圧を適用済み、
  調査中（マルチセグメント TTS の段境界 ResetDecoder / サーバー送信バースト不足 / WS 断）

### v08.05（2026-08-04）

- ファームウェア v1.0.3-aec-wake（`firmware/post-fw-v1.0.3-aec-wake/`）：
  - デバイス側 AEC（ES7210 参照入力でスピーカーエコーを除去）、リスニングを
    Realtime に変更（割り込み可・末尾切れなし）
  - ウェイク高速化：multinet 検出ウィンドウ 3000→1500ms、しきい値下限 0.35→0.30
  - 予熱接続：待機中 WebSocket を常時接続（15s→120s 指数バックオフで再接続）、
    起床時の再ハンドシェイク不要
- Phase 5 決定：P5-1/P5-2（pi/agy の音声確認ループ）は**破棄**——pi は VS Code、
  agy は Antigravity Desktop 経由；P5-4（クラウド能動プッシュ）は**不可**——
  xiaozhi.me にアイドル自発 API なし；P5-5（デスクトップセッション注入）は**不可**——
  codex app-server daemon は Unix のみ、remote-control は SSH ペアリング
- ロールバック用バックアップ：Git tag `backup-v08.04` + `backup-v08.04.zip`

### v08.04（2026-08-04）

- クラウドブリッジ「自動起動 + keep alive」：StackChan-CloudBridge ログオンタスク
  （wscript 非表示）；トレイ内蔵ブリッジ監視（プロセス/ハートビート異常時 30 秒以内に
  非表示で再起動）
- トレイ単一インスタンス保護を強化（`-File` の実インスタンスのみ判定）
- ブリッジ起動チェーン全非表示（VBS → powershell Hidden → python Hidden）
- トレイに「キュー操作」メニュー追加：キューメッセージ表示 / キュー消去（自動バックアップ）/
  未回答の確認問題を消去

### v08.03（2026-08-03）

- クラウドリンク + 起床時アナウンス（prompt v2、agent_pending 起床優先ルール）
- ファームウェア v1.0.2-micfix（マイクゲイン 42、認識改善）
- 4 エージェントの hooks 稼働（codex/claude/agy/pi）、可視ウィンドウ実行
- claude 可視ウィンドウ完了イベント（`agents/claude_visible_run.py`：結果をイベント
  キューと outbox の両方に書き込み、起床時アナウンスと「結果を聞く」の両方に対応）
- 修正：codex Access denied、agent_status タイムアウト（13.9s→4.8s）、中国語文字化け、
  古い結果の読み上げ、claude 完了イベント欠落、claude/pi 作業ディレクトリ、
  トレイ二重表示、スケジュールタスクのウィンドウ点滅
- アーカイブ：`version.08.03/`（当日フルパッケージ）

旧版：`firmware/post-fw-v1.0.0-led`（検証済み 07.31 ビルド、ロールバック可）。

## 参考プロジェクトと謝辞

本ソリューションは以下のオープンソースプロジェクトを参照・利用しています。
各作者に感謝します。

| プロジェクト | 作者 | 用途 |
|---|---|---|
| [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) | @xinnan-tech | 自前 xiaozhi サーバー（予備リンク） |
| [xiaozhi-esp32](https://github.com/78/xiaozhi-esp32) | @78 | デバイスファームウェアの上流 |
| [stackchan-claude-bridge](https://github.com/heavenchenggong/stackchan-claude-bridge) | @heavenchenggong | StackChan × Claude ブリッジファームウェア（検証済み 07.31 ベースの出所） |
| [StackChan-HtSz](https://github.com/mo-hantang/StackChan-HtSz) | @mo-hantang | StackChan-HtSz ファームウェア（メインブランチ） |
| [StackChan](https://github.com/hylarucoder/StackChan) | @hylarucoder | StackChan 参考実装（サーボ/動作/LED） |
| [stackchan-mcp](https://github.com/migratorywhale/stackchan-mcp) | @migratorywhale | StackChan × MCP 参考 |
| [pi-coding-agent](https://github.com/earendil-works/pi-coding-agent) | @earendil-works | pi コーディングエージェント |
| [mcp-calculator](https://github.com/78/mcp-calculator) | @78 | MCP ツール作成のサンプル |

また、各 AI エージェントの公式プロダクト：OpenAI Codex、Anthropic Claude Code、
Google Antigravity（Gemini CLI）。

## 機密情報

このリポジトリには**実際の認証情報は含まれていません**：トークン / API キー /
MAC / ドメインはすべてプレースホルダー（`YOUR_*` / `AA:BB:CC:DD:EE:FF`）。
実際の値はローカルの `.env`、`config.json`、docker 設定のみに存在します。
`.gitignore` は実行時に生成される機密ファイルをすべて除外します。
デプロイ時は [DEPLOY.md](DEPLOY.md) の第 4 節に従い各項目を置き換えてください。

