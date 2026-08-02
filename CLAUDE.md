# CLAUDE.md — AI Secretary 設計ガイド

## プロジェクト概要

Discord 上で動く個人用 AI 秘書 Bot。Claude の Tool Use を使って自然言語でToDo・カレンダー・メールを操作する。

## 実行環境

**Bot は Mac ではなく Windows PC（WSL2）で稼働している。**

| 項目 | 値 |
|---|---|
| 実行環境 | Windows PC の WSL2（Ubuntu 26.04） |
| SSH 接続 | `ssh kaliman@100.114.106.16` |
| WSL2 のユーザー | `kaliman` |
| プロジェクトパス（WSL2） | `/home/kaliman/ai-secretary/` |
| Python 仮想環境 | `/home/kaliman/ai-secretary/.venv/bin/python` |
| systemd サービス名 | `ai-secretary` |
| ログファイル（WSL2） | `/home/kaliman/ai-secretary/ai_secretary.log` |

### よく使うコマンド（WSL2 SSH 後に実行）

```bash
# サービス状態確認
sudo systemctl status ai-secretary

# 再起動
sudo systemctl restart ai-secretary

# ログ確認（リアルタイム）
sudo journalctl -u ai-secretary -f

# ログ確認（直近30行）
sudo journalctl -u ai-secretary -n 30 --no-pager

# ログファイル直接確認
tail -30 /home/kaliman/ai-secretary/ai_secretary.log

# コード更新 → 反映
cd ~/ai-secretary && git pull && sudo systemctl restart ai-secretary
```

### Mac 側でコードを変更したあとの手順

1. Mac で `git push`
2. WSL2 で `git pull && sudo systemctl restart ai-secretary`
3. `.env` の変更は git 管理外なので **WSL2 の `.env` を手動で同期すること**

### Discord チャンネル構成

| チャンネル名 | 用途 | channel ID |
|---|---|---|
| `claude-hisyo` | メイン会話チャンネル | `1505938374639485128` |
| 就活メール通知 | メールバッチ通知・ToDo 候補提示 | `1505938272231096531` |
| 日次レポート | 毎朝7時のレポート送信先 | `1505938308386259135` |
| ToDo | ToDo 一覧表示 | `1505938339759653084` |
| クレジット確認 | Anthropic 残高アラート | `1506198964230164520` |
| 就活リサーチ | 志望業界の新着ニュースを毎日リサーチ・投稿 | `1533387135573295235` |

`config.py` の `DISCORD_ALLOWED_CHANNEL_IDS` に含まれていないチャンネルはメッセージを**無視する**（ログにも残らない）。

## アーキテクチャ

```
main.py
├── on_ready         → スケジューラー起動 + 起動補完送信
├── on_message       → handlers/message_handler.py へ委譲
└── commands         → !report, !todo

schedulers/scheduler.py   → APScheduler ジョブ定義

handlers/
├── message_handler.py    → Discord メッセージ受信・Tool Use ディスパッチ・会話履歴管理
├── report_handler.py     → 日次レポート生成・送信
├── todo_handler.py       → ToDo CRUD の Discord 応答
├── job_email_handler.py  → 就活メールバッチ処理
└── job_research_handler.py → 就活リサーチバッチ処理（Web検索）

services/
├── claude_service.py     → Anthropic API 呼び出し・Tool 定義・各種プロンプト
├── supabase_service.py   → DB アクセス（TTL キャッシュ付き）
├── gmail_service.py      → Gmail API（読み取り専用）
├── calendar_service.py   → Google Calendar API
└── notion_service.py     → Notion API
```

## スケジューラー

| ジョブ ID | 処理 | 時刻 |
|---|---|---|
| `daily_report` | 日次レポート生成・Discord 送信 | 毎日 0:00（`DAILY_REPORT_HOUR` / `DAILY_REPORT_MINUTE` で変更可） |
| `job_email_batch` | 就活メール取得・スコアリング・Discord 通知 | 毎日 0:00 |
| `job_research_batch` | 志望業界の新着ニュースをWeb検索・Discord 通知 | 毎日 0:00 |
| `notion_todo_sync` | Supabase → Notion の未同期 ToDo を同期 | 毎日 0:00 |
| `credit_check` | Anthropic クレジット残高アラート | 1 時間ごと |

`daily_report` は `misfire_grace_time=3600, coalesce=True` 設定済み。  
`DAILY_REPORT_HOUR` / `DAILY_REPORT_MINUTE` はデフォルト `0` / `0`。

### 起動補完送信（main.py `on_ready`）

bot が 0:00 以降に起動し、かつ当日未送信なら日次レポートを補完送信する。  
`is_sent_today()` は `/tmp/ai_secretary_report_sent.txt` の日付で判定する（プロセス再起動でリセットされない）。  
**`generate_and_post` 自体に `asyncio.Lock` + 送信済みチェックがあるため、`on_ready` が複数回発火しても重複しない。**

## Tool Use フロー（メッセージ処理）

```
on_message
 └─ message_handler._handle_inner
      ├─ _load_history_if_needed  (Supabase から会話履歴を復元、初回のみ)
      ├─ _get_history              (直近 12 件をサニタイズ)
      ├─ claude_service.chat_with_tools(content, history)
      │    └─ Anthropic API 1 回のコールで意図判定 + ツール選択
      └─ result["type"] == "tool_use"
           ├─ history に assistant(tool_use) を追加
           ├─ _dispatch_tool → 各 handler/service を呼ぶ
           ├─ history に user(tool_result) を追加
           ├─ history に assistant(サマリー文字列) を追加
           └─ Supabase に会話履歴を保存
```

## 会話履歴の構造

インメモリ（`_conversation_history: dict[int, list[dict]]`）+ Supabase に永続化。

```
user(str)                       # ユーザー発話
assistant(list)                 # [{"type":"tool_use","id":...}, ...]
user(list)                      # [{"type":"tool_result","tool_use_id":...}]
assistant(str)                  # ツール結果のサマリー（APIコールなし）
```

**不変条件**: `tool_use` ブロックと `tool_result` は 1:1 対応しなければならない。  
`assistant_content` には実行する 1 件の `tool_use` のみ含める（複数返ってきた場合は最初の 1 件に絞る）。  
`_sanitize_history` と `chat_with_tools` 内のクリーンアップは両方、全 `tool_use` ID が対応 `tool_result` を持つかを検証してから保存。

## Supabase テーブル

| テーブル | 用途 |
|---|---|
| `user_profile` | ユーザー情報・設定（1行）|
| `todos` | ToDo（`source`, `source_id`, `synced_to_notion` カラムあり）|
| `email_logs` | 就活メール処理済みログ（重複防止用）|
| `custom_rules` | メール処理ルール（sender / subject）|
| `contacts` | 重要連絡先 |
| `conversation_history` | チャンネルごとの会話履歴（JSON）|
| `decision_history` | 意思決定ログ（現状未使用）|
| `event_logs` | カレンダー登録ログ |
| `research_logs` | 就活リサーチで通知済みの記事 URL ログ（重複防止用）|

`supabase_service` は `user_profile` / `custom_rules` / `contacts` に TTL 5 分のインメモリキャッシュを持つ。

## 就活メールバッチ処理（5 ステップ）

1. 件名・送信者のみ取得（本文なし、直近 24h・最大 500 件）
2. Claude でスコアリング（1〜5）、10 件ずつバッチ処理
3. スコア閾値以上のメールのみ本文取得（デフォルト閾値 4）
4. 要約・ToDo 候補を並列生成、`email_logs` に保存
5. Discord に投稿 → ToDo 候補を提示（ユーザーが番号を選択して追加）

`email_score_threshold` は `user_profile.preferences.email_score_threshold` で変更可能。

## 就活リサーチバッチ処理

1. `user_profile.priorities`（志望業界・優先事項）を取得
2. Claude の Web 検索ツール（`web_search_20250305`、サーバー側実行）で直近24〜48hの関連ニュースを検索、JSON配列で3〜6件に厳選
3. `research_logs.url` で重複チェック → 未通知の記事のみ抽出
4. 新規記事を `research_logs` に保存
5. Discord（就活リサーチチャンネル）に投稿。新着が無い日は「なかったよ」メッセージのみ

`priorities` が未設定の場合は「IT・Web業界」をデフォルトの検索対象とする。

## カレンダー登録フロー（確認ステップあり）

1. `add_event` ツール → `_pending_events[channel_id]` に仮登録
2. ユーザーが「OK」→ Google Calendar + Notion に登録
3. 「キャンセル」または修正日時の自然言語入力に対応

## 主要な設計上の注意点

- **`DAILY_REPORT_HOUR=0` のとき `on_ready` の `report_due` 条件が常に True になる**。  
  `now.hour > 0` は日中すべて真のため。補完送信の重複防止は `generate_and_post` 内のロックに任せる。

- **Claude が複数 `tool_use` を1レスポンスで返す可能性がある**。  
  `chat_with_tools` は最初の 1 件のみ処理し、`assistant_content` も 1 件に絞って保存する。

- **会話履歴は `_MAX_HISTORY=20` でトリミングされる**。  
  `_get_history` は直近 12 件をさらに絞り、最初のユーザー文字列メッセージから返す（tool サイクル途中からのスライスを防ぐ）。

- **`/tmp` は OS 再起動でクリアされる**。  
  `is_sent_today()` が False にリセットされるため、再起動直後に補完送信が走ることがある（意図した動作）。
