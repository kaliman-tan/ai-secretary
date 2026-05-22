# AI Secretary

Discord 上で動く個人用 AI 秘書 Bot です。Claude AI を使って、日常のタスク管理・スケジュール管理・メール管理を Discord から自然言語で操作できます。

## 主な機能

### 日次レポート（自動送信）
毎日深夜0時に以下をまとめたレポートを Discord に送信します。
- 今日・明日の Google Calendar / Notion の予定
- 未完了の ToDo リスト
- 直近の就活メール
- API クレジット残高

### ToDo 管理
自然な日本語で話しかけるだけで操作できます。
- 「〜をリストに追加して」→ ToDo を追加
- 「〜終わったよ」→ ToDo を完了にする
- 「ToDoリスト見せて」→ 一覧表示
- 期限の指定も自然言語で OK（例：「明日の17時までに〜」）

### カレンダー管理
- 予定の登録（確認ステップあり）
- 「今日の予定は？」「今週の予定見せて」などで一覧表示
- Google Calendar と Notion カレンダーに同時登録

### 就活メール管理
- Gmail の就活ラベルを定期チェック（15分ごと）
- 重要度スコアでフィルタリングして Discord に通知
- メールルールのカスタマイズ

### クレジット残高アラート
Anthropic API のクレジット残高を1時間ごとにチェックし、残りわずかになったら Discord で通知します。

## 技術スタック

| カテゴリ | 使用技術 |
|---------|---------|
| AI | Anthropic Claude API |
| Discord | discord.py |
| カレンダー | Google Calendar API |
| メール | Gmail API |
| メモ管理 | Notion API |
| データベース | Supabase |
| スケジューラー | APScheduler |
| インフラ | Docker / Google Cloud Run |

## セットアップ

### 必要なもの
- Python 3.12+
- 各種 API キー（下記参照）

### 環境変数
`.env.example` をコピーして `.env` を作成し、各値を設定してください。

```bash
cp .env.example .env
```

| 変数名 | 説明 |
|--------|------|
| `DISCORD_BOT_TOKEN` | Discord Bot のトークン |
| `DISCORD_GUILD_ID` | 使用するサーバーの ID |
| `ANTHROPIC_API_KEY` | Anthropic API キー |
| `NOTION_API_KEY` | Notion インテグレーションキー |
| `SUPABASE_URL` | Supabase プロジェクト URL |
| `SUPABASE_KEY` | Supabase API キー |
| `DAILY_REPORT_HOUR` | 日次レポート送信時刻（時）デフォルト: `0` |

### インストールと起動

```bash
pip install -r requirements.txt
python main.py
```

### tmux で常時起動する場合

```bash
tmux new-session -d -s ai-secretary 'python main.py'
```
