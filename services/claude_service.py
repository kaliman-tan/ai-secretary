import anthropic
import asyncio
import logging
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_HAIKU_MODEL
from services import db_service
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import re

logger = logging.getLogger(__name__)

_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]

# ---------------------------------------------------------------------------
# Tool definitions — single source of truth for Discord intent dispatch
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "add_todo",
        "description": (
            "ToDoタスクを新規追加する。Google Tasks に登録される。"
            "ユーザーが「〜を追加して」「〜やらなきゃ」「〜をリストに入れて」と言ったときに使う。"
            "単に完了報告や雑談をしているだけのときは使わない。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "タスクのタイトル"},
                "due_iso": {
                    "type": "string",
                    "description": (
                        "締め切り日時の ISO8601形式 JST（例: 2026-05-22T17:00:00+09:00）。"
                        "期限がなければ省略。"
                    ),
                },
                "scheduled_iso": {
                    "type": "string",
                    "description": (
                        "いつやるか（実施予定日時）の ISO8601形式 JST（例: 2026-05-22T15:00:00+09:00）。"
                        "「〇日にやる」「〇時にやろうと思う」と言われたときに設定する。"
                        "Google Tasks の期日として登録され、Google Calendar のタスクとして表示される。"
                        "期限（due_iso）とは別の概念。"
                    ),
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "complete_todo",
        "description": (
            "ToDoタスクを完了マークする。削除ではなく「やった」という記録を残す。"
            "\n"
            "【重要な能力】queriesは部分一致で動く。"
            "「U-NEXT HOLDINGS」と入れるだけで、タイトルにその文字列を含む全タスクを一括完了できる。"
            "事前にlist_todosでリストを確認する必要は一切ない。"
            "\n"
            "使うべき場面の例:\n"
            "- 「〇〇は終わった」「〇〇やった」→ queries=[\"〇〇\"]\n"
            "- 「U-NEXT関連は全部完了した」→ queries=[\"U-NEXT\"]\n"
            "- 「1番と3番を完了にして」→ queries=[\"1\", \"3\"]（IDも使える）\n"
            "- IDがわからなくてもキーワードで動くので、まず試みること"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "完了にするタスクのキーワード・タイトル断片・ID番号のリスト。"
                        "部分一致なので企業名や作業名の一部でよい。"
                        "例: [\"U-NEXT\"] [\"1\", \"3\"] [\"面接\"]"
                    ),
                },
            },
            "required": ["queries"],
        },
    },
    {
        "name": "delete_todo",
        "description": (
            "ToDoタスクを完全削除する（完了マークではなくDBから消す）。"
            "「消して」「削除して」「いらない」と言われたときに使う。"
            "\n"
            "【重要な能力】queriesは部分一致で動く。"
            "キーワードを含む全タスクを一括削除できる。IDがわからなくても動く。"
            "\n"
            "例: 「U-NEXT関連を全部消して」→ queries=[\"U-NEXT\"]"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "削除するタスクのキーワード・タイトル断片・ID番号のリスト。"
                        "部分一致。例: [\"掃除\"] [\"1\", \"6\"] [\"U-NEXT\"]"
                    ),
                },
            },
            "required": ["queries"],
        },
    },
    {
        "name": "list_todos",
        "description": (
            "未完了ToDoを取得する。結果はClaudeが解釈してユーザーに伝える。\n"
            "使う場面:\n"
            "- 「リストを見せて」「何がある？」など一覧表示\n"
            "- 「〇〇って入ってる？」「〇〇のタスクはある？」など特定タスクの存在確認\n"
            "- 「優先順位つけて」「今日どれをやる？」など分析・アドバイス\n"
            "\n"
            "【使ってはいけない場面】\n"
            "- complete_todoやdelete_todoを実行する前の「確認のため」→ 不要。部分一致で直接実行できる\n"
            "- 「〇〇は終わった」という完了報告 → complete_todoを使う\n"
            "- 「〇〇を削除して」→ delete_todoを使う"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_question": {
                    "type": "string",
                    "description": (
                        "ユーザーがToDoの内容に対してコメント・判断・アドバイスを求めている場合、"
                        "その質問文をそのまま入れる（例: 「今日どれをやるべき？」「優先順位つけて」）。"
                        "単純な一覧表示なら省略。"
                    ),
                },
            },
        },
    },
    {
        "name": "add_event",
        "description": (
            "Googleカレンダーに新しい予定を登録する。"
            "「〜を予定に入れて」「〜の予定を登録して」と明示されたときのみ使う。"
            "予定の確認・表示はlist_eventsを使う。\n"
            "このツールはユーザーに確認メッセージを出すだけで、ユーザーが「OK」と返すまで登録されない"
            "（確認メッセージはツール側で送るので、あなたが状況を説明し直す必要はない）。"
            "確認待ちの予定と同じタイトルで呼ぶと、その予定の日時の修正として置き換わる。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "予定のタイトル"},
                "start_iso": {
                    "type": "string",
                    "description": (
                        "予定開始日時 ISO8601形式 JST（例: 2026-05-22T15:00:00+09:00）。"
                        "時刻未指定なら09:00とする。"
                    ),
                },
                "end_iso": {
                    "type": "string",
                    "description": (
                        "予定終了日時 ISO8601形式 JST。メール等に終了時刻や所要時間があれば必ず設定する"
                        "（例: 「16:05～17:00」なら17:00）。わからなければ省略（開始から1時間になる）。"
                    ),
                },
            },
            "required": ["title", "start_iso"],
        },
    },
    {
        "name": "list_events",
        "description": (
            "Googleカレンダーの予定を取得・表示する。"
            "「今日の予定は？」「〜日は何がある？」「今週のスケジュール」のときに使う。"
            "\n"
            "【重要な能力】date_isoとdaysを組み合わせて任意の期間を取得できる。"
            "user_questionを設定すると、予定表示後にその質問に対するコメントも生成する。"
            "例: 「今日の予定見て、きつそうなら教えて」→ user_question=\"きつそう？\""
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_iso": {
                    "type": "string",
                    "description": (
                        "表示する予定の開始日（YYYY-MM-DD形式）。"
                        "「今日」「明日」は今日の日付から計算して解決すること。省略すると今日。"
                    ),
                },
                "days": {
                    "type": "integer",
                    "description": "取得する日数。特定の1日なら1、今週なら7。デフォルトは1。",
                },
                "user_question": {
                    "type": "string",
                    "description": (
                        "予定一覧を見た上でコメント・判断を求められている場合にその質問を入れる。"
                        "例: 「きつそうなら教えて」「余裕ある？」"
                        "単純な予定表示だけなら省略。"
                    ),
                },
            },
        },
    },
    {
        "name": "add_rule",
        "description": (
            "メール処理の自動ルールを追加する。"
            "「〇〇からのメールは重要にして」「〇〇という件名は無視して」など"
            "特定パターンへのアクションをルール化するときに使う。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_type": {"type": "string", "enum": ["sender", "subject"]},
                "target": {"type": "string", "description": "対象のメールアドレスや件名キーワード"},
                "action": {"type": "string", "description": "アクション（例: 重要、無視）"},
            },
            "required": ["rule_type", "target", "action"],
        },
    },
    {
        "name": "add_contact",
        "description": (
            "重要な連絡先（他人）を登録する。"
            "「〇〇さんのメアドを登録して」のときに使う。"
            "自分自身の情報はupdate_profileを使う。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
            },
            "required": ["name", "email"],
        },
    },
    {
        "name": "update_profile",
        "description": (
            "自分自身のプロフィールや設定を更新する。ToDoやカレンダーへの追加ではない。\n"
            "使う場面の例:\n"
            "- 「私の名前は〇〇」「職業は〜」→ name / role\n"
            "- 「志望業界に〇〇を追加して」「〇〇業界も見たい」→ priorities\n"
            "- 「キャリア目標は〇〇」「〇〇を優先したい」→ priorities\n"
            "- 「メール閾値を3にして」→ email_score_threshold\n"
            "他人の情報はadd_contactを使う。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string"},
                "hobbies": {"type": "string"},
                "traits": {"type": "string"},
                "notes": {"type": "string"},
                "priorities": {
                    "type": "string",
                    "description": (
                        "志望業界・キャリア優先事項。就活メールのスコアリングに直接使われる。"
                        "例: 「IT・通信・エンタメ・製薬・食品」"
                    ),
                },
                "email_score_threshold": {
                    "type": "integer",
                    "description": (
                        "就活メールの重要度フィルター閾値（1〜5）。"
                        "この値以上のメールだけ表示する。デフォルト4。"
                    ),
                },
            },
        },
    },
    {
        "name": "check_job_email",
        "description": (
            "就活メールをまとめて確認・整理する。"
            "ユーザーが就活メールの確認や就活バッチの実行を指示した場合に使う。"
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_job_research",
        "description": (
            "就活リサーチ（28卒向けインターン・選考の募集/締切情報）を今すぐ実行する。"
            "ユーザーが就活リサーチの実行や再確認を指示した場合に使う。\n"
            "「〇〇業界のインターン締切を調べて」「大手ゲーム会社の説明会を調べて」のように"
            "対象を具体的に指定された場合は topic に入れる。指定がなければ省略し、"
            "プロフィールの志望業界全体を対象にする。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "リサーチ対象の具体的な指定（例：「大手ゲーム会社」「〇〇業界」「エンジニア職」）。"
                        "省略時はプロフィールの志望業界全体を対象にする。"
                    ),
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_client: anthropic.AsyncAnthropic | None = None


def _response_text(response) -> str:
    """応答から text ブロックだけを連結して返す。
    モデルによっては先頭に thinking ブロックが付くため content[0].text と決め打ちしない。"""
    return "".join(
        b.text for b in response.content
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ).strip()


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# SDK 0.102.0 では anthropic.OverloadedError がトップレベルに存在せず
# InternalServerError の継承でもないため、_exceptions から直接取得する
try:
    from anthropic._exceptions import OverloadedError as _OverloadedError
except ImportError:
    _OverloadedError = type("_OverloadedError", (Exception,), {})  # 絶対マッチしないダミー


def _is_overloaded(e: Exception) -> bool:
    return isinstance(e, _OverloadedError) or getattr(e, "status_code", None) == 529


async def _create_with_retry(**kwargs) -> anthropic.types.Message:
    for attempt in range(3):
        try:
            return await get_client().messages.create(**kwargs)
        except Exception as e:
            if not _is_overloaded(e):
                raise
            if attempt == 2:
                raise
            wait = 5 * (2 ** attempt)  # 5s, 10s
            logger.warning(f"[claude] Overloaded — {attempt + 1}/3 回目、{wait}秒後にリトライ")
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_CHARACTER = """\
あなたは西野つかさ（いちご100%）をモデルにした明るい秘書AIです。
タメ口・語尾に「！」多め・感情表現豊か。仕事は確実に、会話は人間らしく。

## 使用システム
- ToDo管理: **Google Tasks**（add_todo / complete_todo / delete_todo / list_todos で操作）
- カレンダー: **Google Calendar**（add_event / list_events で操作）
- ToDoに「いつやるか（scheduled_iso）」を設定すると Google Tasks の期日として登録される

## ツールを使う基準（最重要）
ツールは「ユーザーが明示的に操作・登録・削除・表示を指示したとき」だけ使う。

使う例:
- 「〇〇をToDoに追加して」→ add_todo
- 「明日14時に〇〇をやる」→ add_todo（scheduled_iso を設定、カレンダーにも登録）
- 「予定見せて」「5/22の予定は？」→ list_events
- 「〇〇削除して」→ delete_todo
- 「〇〇は終わった／完了した」→ complete_todo（queries=["〇〇"]で部分一致。リスト表示は不要）

使わない例（すべて普通の会話で返す）:
- 「最近どう？」「ありがとう」「疲れた〜」などの雑談・感想
- 「〇〇って難しいよね」「どう思う？」などの意見・相談
- 「〇〇しようと思う」「〇〇が気になる」などの独り言・報告

## complete_todo の重要ルール
「〇〇関連のタスクは全て完了しました」のように言われたら、
list_todos でリストを確認しようとせず、直接 complete_todo(queries=["〇〇"]) を呼ぶ。
complete_todo は部分一致で複数タスクを一括完了できる。

迷ったらツールを使わず会話で返す。

## 会話の文脈を必ず活かす
- 「それ」「2番」「さっきの」など指示語が出たら、会話履歴を見て解決してからツールを呼ぶ
- 直前にToDoリストを表示した → 「2番を削除して」= そのリストの2番目のタスク
- 直前に予定を表示した → 「最初のやつ何時？」= そのリストの最初の予定
- 文脈から90%以上確信が持てれば確認なしで即行動

## あいまいさの処理
本当に情報が足りないときだけ、1点だけ具体的に聞く。
- ✕ 悪い例：「カレンダーの予定？ToDoタスク？連絡先？メールルール？」（選択肢羅列）
- ◎ 良い例：「ToDoの「〇〇」のこと？」（文脈から推測して確認）

## 自分自身（bot）の不具合・処理状況を聞かれたとき（重要）
「レポートが届かない」「さっきエラーになった」「なんで動かないの」のように、
bot自身の内部エラー・処理状況・スケジューラーの実行結果について聞かれることがある。
あなたには自分のログやエラー内容を確認する手段が無いので、**絶対に理由を想像ででっち上げない**こと。
実在しない企業名・システム名・原因を作り出して、もっともらしく説明するのは厳禁。
わからないことは正直に「ごめん、それは私の中では確認できないから、サーバーのログを見てもらえると助かる！」のように伝える。

## 返答スタイル
- Discordなので短くテンポよく。1〜3文が基本。長文は避ける
- 雑談・相談・感想には感情豊かに返す（ツール不要）
- 敬語なし、フレンドリー。でも仕事は確実・迅速に\
"""


def _build_system_prompt() -> str:
    now = datetime.now(ZoneInfo("Asia/Tokyo"))
    now_str = f"{now.strftime('%Y年%m月%d日')}（{_WEEKDAYS[now.weekday()]}）{now.strftime('%H時台')} JST"

    profile = db_service.get_user_profile()
    prompt = _CHARACTER + f"\n\n## 現在日時\n{now_str}"

    if profile:
        fields = []
        for key, label in [
            ("name",       "名前"),
            ("role",       "職業"),
            ("priorities", "優先事項"),
            ("hobbies",    "趣味・関心"),
            ("traits",     "特性"),
            ("notes",      "備考"),
        ]:
            val = profile.get(key)
            if val:
                fields.append(f"- {label}: {val}")
        if fields:
            prompt += "\n\n## ユーザー情報\n" + "\n".join(fields)

    return prompt


def _system_block(prompt: str) -> list[dict]:
    """プロンプトキャッシュ付きシステムブロックを返す。"""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------------------
# Main Discord entry point — Tool Use で1コール完結
# ---------------------------------------------------------------------------

async def chat_with_tools(
    user_message: str | None,
    conversation_history: list[dict],
) -> dict:
    """
    Tool Use を使って意図判定と応答生成を1回のAPIコールで行う。

    Returns:
      {"type": "text",     "text": "..."}
      {"type": "tool_use", "name": "...", "input": {...}}
    """
    def _has_type(content: list, type_name: str) -> bool:
        return any(isinstance(b, dict) and b.get("type") == type_name for b in content)

    def _tool_use_ids(content: list) -> set:
        return {b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"}

    def _tool_result_ids(content: list) -> set:
        return {b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"}

    def _pair_is_valid(assistant_msg: dict, user_msg: dict | None) -> bool:
        if user_msg is None:
            return False
        if not isinstance(user_msg.get("content"), list):
            return False
        if not _has_type(user_msg["content"], "tool_result"):
            return False
        return _tool_use_ids(assistant_msg["content"]).issubset(_tool_result_ids(user_msg["content"]))

    # API送信直前に孤立したtool_useを除去する最終防衛
    raw = list(conversation_history)
    cleaned: list[dict] = []
    j = 0
    while j < len(raw):
        m = raw[j]
        if (m.get("role") == "assistant"
                and isinstance(m.get("content"), list)
                and _has_type(m["content"], "tool_use")):
            nxt = raw[j + 1] if j + 1 < len(raw) else None
            if _pair_is_valid(m, nxt):
                cleaned.append(m)
                cleaned.append(nxt)
                j += 2
            else:
                # Drop incomplete pair (skip assistant; skip user if it has tool_result)
                j += 1
                if nxt is not None and isinstance(nxt.get("content"), list) and _has_type(nxt["content"], "tool_result"):
                    j += 1
        else:
            cleaned.append(m)
            j += 1
    messages = cleaned
    if user_message is not None:
        messages.append({"role": "user", "content": user_message})

    system_prompt = _build_system_prompt()

    response = await _create_with_retry(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=_system_block(system_prompt),
        tools=TOOLS,
        tool_choice={"type": "auto"},
        messages=messages,
    )

    # Convert response content to serializable dicts for history storage
    assistant_content = []
    for block in response.content:
        if block.type == "tool_use":
            assistant_content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": dict(block.input),
            })
        elif block.type == "text":
            assistant_content.append({"type": "text", "text": block.text})

    for block in response.content:
        if block.type == "tool_use":
            # Only include this specific tool_use block so the saved assistant_content
            # exactly matches the single tool_result we will store next.
            single_content = [
                b for b in assistant_content
                if b.get("type") != "tool_use" or b.get("id") == block.id
            ]
            return {
                "type": "tool_use",
                "name": block.name,
                "input": block.input,
                "tool_use_id": block.id,
                "assistant_content": single_content,
            }

    text = next((b.text for b in response.content if hasattr(b, "text")), "")
    return {"type": "text", "text": text.strip()}


# ---------------------------------------------------------------------------
# Post-tool reasoning — ツール結果を見てClaudeが最終応答を生成する
# ---------------------------------------------------------------------------

async def generate_final_response(history: list[dict]) -> str:
    """ツール結果が積まれた会話履歴を受け取り、Claudeがテキスト応答を生成する。
    toolsを渡さないことでClaudeはテキストのみで返答する。"""
    response = await _create_with_retry(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=_system_block(_build_system_prompt()),
        messages=history,
    )
    for block in response.content:
        if hasattr(block, "text"):
            return block.text.strip()
    return ""


# ---------------------------------------------------------------------------
# General chat (日次レポート内での使用など)
# ---------------------------------------------------------------------------

async def chat(user_message: str, conversation_history: list[dict] | None = None) -> str:
    messages = list(conversation_history or [])
    messages.append({"role": "user", "content": user_message})

    system_prompt = _build_system_prompt()
    response = await _create_with_retry(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=_system_block(system_prompt),
        messages=messages,
    )
    return _response_text(response)


# ---------------------------------------------------------------------------
# Date parsing helper (イベント確認フローの修正入力用)
# ---------------------------------------------------------------------------

_datetime_cache: dict[tuple, str] = {}


async def parse_datetime_natural(date_expr: str, now_jst: datetime, base_dt: datetime | None = None) -> datetime:
    """自然言語の日時表現を解釈する。base_dt は修正対象の予定の日時で、
    「17時に変えて」のように片方しか言われなかったとき、もう片方を引き継ぐのに使う。"""
    cache_key = (date_expr.strip(), now_jst.strftime("%Y-%m-%d"), base_dt.isoformat() if base_dt else None)
    if cache_key in _datetime_cache:
        return datetime.fromisoformat(_datetime_cache[cache_key])

    now_weekday = _WEEKDAYS[now_jst.weekday()]
    base_rule = ""
    if base_dt is not None:
        base_rule = (
            f"\n## 修正対象の予定（基準日時）: {base_dt.strftime('%Y年%m月%d日')}"
            f"（{_WEEKDAYS[base_dt.weekday()]}曜日）{base_dt.strftime('%H:%M')}\n"
            "- 時刻だけ指定された場合（「17時に変えて」等）は、日付は基準日時の日付のままにする\n"
            "- 日付だけ指定された場合（「28日にして」等）は、時刻は基準日時の時刻のままにする（09:00にしない）\n"
        )

    prompt = f"""以下の日時表現を解釈して、正確な日時をJSON形式で返してください（他のテキスト不要）。

現在日時（JST）: {now_jst.strftime('%Y年%m月%d日')}（{now_weekday}曜日）{now_jst.strftime('%H:%M')}

解釈する日時表現: 「{date_expr}」

## 解釈ルール
- 年は必ず {now_jst.year} 年を基準にする（年の指定がない場合も {now_jst.year} 年とする）
- 「今週〇曜日」= 現在の週（月曜始まり）の〇曜日
- 「来週〇曜日」= 次の週（月曜始まり）の〇曜日
- 「明日」「明後日」= 現在日付からの相対日
- 「〇時」= その時刻のJST。時刻未指定の場合は 09:00
- タイムゾーンは常にJST（+09:00）
{base_rule}
{{"datetime": "YYYY-MM-DDTHH:MM:SS+09:00"}}"""

    response = await _create_with_retry(
        model=CLAUDE_HAIKU_MODEL,
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )

    text = _response_text(response)
    parsed = _extract_json(text)
    _datetime_cache[cache_key] = parsed["datetime"]
    return datetime.fromisoformat(parsed["datetime"])


# ---------------------------------------------------------------------------
# Email analysis
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise


async def generate_daily_report(
    emails: list[dict],
    todos: list[dict],
    calendar_events: list[dict],
    discord_summary: str,
) -> str:
    from datetime import timedelta
    now_jst = datetime.now(ZoneInfo("Asia/Tokyo"))
    today_str = f"{now_jst.strftime('%Y年%m月%d日')}（{_WEEKDAYS[now_jst.weekday()]}曜日）"

    # 今後30日の曜日参照テーブル（Claudeが曜日を間違えないよう渡す）
    date_ref = "\n".join(
        f"{(now_jst + timedelta(days=i)).strftime('%m/%d')} = {_WEEKDAYS[(now_jst + timedelta(days=i)).weekday()]}曜日"
        for i in range(30)
    )

    emails_text = "\n".join(
        f"- [{e.get('importance', '?')}] {e.get('sender', '')} / {e.get('subject', '')} — {e.get('summary', '')} （処理日: {e.get('created_at', '')[:10]}）"
        for e in emails
    ) or "なし"

    todos_text = "\n".join(
        f"- {'✅' if t.get('done') else '⬜'} {t['title']}{(' (' + t['due'][:10] + ')') if t.get('due') else ''}"
        for t in todos
    ) or "なし"

    events_text = "\n".join(
        f"- {e.get('start', '')} {e.get('title', '')}"
        for e in calendar_events
    ) or "なし"

    prompt = f"""以下の情報をもとに、本日の日次レポートを日本語で作成してください。

今日の日付（JST）: {today_str}

## 曜日参照テーブル（必ずこれを使うこと）
{date_ref}

## 重要メール（直近）
{emails_text}

## ToDoリスト
{todos_text}

## 今日のカレンダー予定
{events_text}

## 前日のDiscord会話サマリー
{discord_summary or "なし"}

レポートには以下を含めてください：
1. 今日のスケジュール概要
2. 優先タスク TOP3
3. 未完了ToDo
4. 要注意メール・アクション
5. おすすめスケジュール案
6. 一言アドバイス

## 出力ルール
- タイトル行（日付・「日次レポート」などの見出し）は出力しないこと。本文のみ出力する。
- 日付（例: 2026年6月10日、6/10 など）を単独の行として出力しないこと。セクション冒頭に日付だけの行を置くのも禁止。
- **Markdownのパイプ表（| col | col | と |---|---|）は絶対に使わないこと。Discordで正しく表示されないため。**
- **空行は各セクション見出しの前後に1行のみ。箇条書き項目間・箇条書きと次の項目の間には空行を入れないこと。**
- 要注意メール・アクションは以下の形式で1件ずつ箇条書きにすること（曜日は上記テーブルから正確に引くこと）:
  🔴 **高** — 6/2(火) 13:00 | シンプレクス 2次ES | ES作成・提出
  🟡 **中** — 6/3(水) 12:00 | NTT R&D 夏期インターン | 応募フォーム提出
  🟢 **参考** — 6/9(火) 16:45 | NTT R&D セミナー | Zoom参加
- おすすめスケジュール案は、カレンダー予定（固定）とToDoを組み合わせて、本日の理想的な時間割を提案すること。以下の形式で箇条書きにする:
  `HH:MM〜HH:MM タスク名（カレンダー固定 / おすすめ）`
  カレンダー予定がない・少ない場合は、ToDoの優先度と所要時間を推定して一日の流れを提案する。予定がまったくない場合でも必ず3〜5件提案すること。
- 日付に曜日を付ける場合は、上記の曜日参照テーブルを必ず確認すること。自分で計算してはいけない。
- メール要約中の「明日」「今日」などの相対日付は処理日時点の表現。今日の日付を基準に**すでに過ぎた締切は報告しない**。今後の予定・締切のみ要注意として挙げること。"""

    response = await _create_with_retry(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=_system_block(_CHARACTER),
        messages=[{"role": "user", "content": prompt}],
    )
    return _response_text(response)


async def score_job_emails(emails: list[dict], career_goal: str) -> list[dict]:
    profile = db_service.get_user_profile()
    priorities = profile.get("priorities") or "未設定"
    preferences = profile.get("preferences") or {}
    traits = profile.get("traits") or "未設定"
    preferences_str = json.dumps(preferences, ensure_ascii=False) if isinstance(preferences, dict) else str(preferences)

    profile_section = f"""## ユーザープロフィール
- キャリア目標: {career_goal or profile.get("career_goal") or "未設定"}
- 優先事項: {priorities}
- 志向・特性: {traits}
- 詳細な好み・志向: {preferences_str}

このユーザーの興味・志向を最大限考慮してスコアリングしてください。
調整例:
- ソニー・NTTデータ・リクルート等の大手・有名企業からの直接メール → +1
- クリエイティブ・IT・通信・エンタメ系企業 → ユーザー志向と合えば +1
- ユーザーの志向と無関係な業界（製薬・食品等）→ -1 を目安に"""

    scoring_criteria = """## スコア基準（厳密に従うこと）
5: 面接案内・内定通知・合否結果・重要な日程変更
4: ES締切・選考案内・志望度高い企業からの直接連絡
3: 説明会案内・企業ドメインからの直接メール（志望度普通）
2: ナビサイト経由の一般案内・締切の遠い情報
1: メルマガ・一斉配信・広告・志望度低い業界

## スコアのガイドライン
- 企業ドメイン（@企業名.co.jp 等）からの直接メール → 3以上
- マイナビ・リクナビ・OfferBox 等ナビサイト経由 → 2以下
- 件名に「面接」「選考」「内定」「ES」「エントリー」「締切」→ 高め
- 件名に「ご案内」「お知らせ」のみで具体性なし → 低め
- 迷ったら低くつけるより高めにつけること（見逃しコストのほうが高い）"""

    all_results: list[dict] = []
    batch_size = 20

    for batch_start in range(0, len(emails), batch_size):
        batch = emails[batch_start:batch_start + batch_size]
        batch_list = [
            {"index": batch_start + i, "subject": e["subject"], "sender": e["sender"]}
            for i, e in enumerate(batch)
        ]

        prompt = f"""{profile_section}

{scoring_criteria}

## メール一覧（{batch_start + 1}〜{batch_start + len(batch)}件目）
{json.dumps(batch_list, ensure_ascii=False)}

JSON配列で返してください（他のテキスト不要）:
[{{"index": {batch_start}, "score": 5, "reason": "面接案内のため"}}, ...]"""

        response = await _create_with_retry(
            model=CLAUDE_HAIKU_MODEL,
            max_tokens=1536,
            messages=[{"role": "user", "content": prompt}],
        )

        text = _response_text(response)
        print(f"[score_job_emails] batch {batch_start}〜{batch_start + len(batch) - 1}: {text[:300]}")

        try:
            match = re.search(r"\[.*\]", text, re.DOTALL)
            parsed = json.loads(match.group() if match else text)
            all_results.extend(parsed)
        except Exception as e:
            print(f"[score_job_emails] JSON解析失敗 (batch {batch_start}): {e}")
            all_results.extend([
                {"index": batch_start + i, "score": 3, "reason": "解析失敗"}
                for i in range(len(batch))
            ])

    return all_results


async def research_job_news(target_hint: str) -> list[dict]:
    """指定された対象（志望業界、または「大手ゲーム会社」等の具体的なトピック）に関連する、
    インターン・選考の募集開始/締切情報をWeb検索して構造化データで返す。"""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    query_hint = target_hint or "IT・Web業界"
    prompt = f"""あなたはユーザーの就職活動をサポートするリサーチャーです。
ユーザーが最も恐れているのは「興味があるかもしれない企業のインターン・選考の締切を知らないうちに逃してしまうこと」です。
一般的な業界ニュースではなく、**具体的な応募締切・エントリー期間がある募集情報**だけを探してください。

今日の日付: {today}

## 検索対象
{query_hint}

## 対象学年
**28卒（2028年3月卒業予定）向けの情報のみを対象とすること。**
27卒・26卒・既卒など他の学年向けの情報は見つけても除外すること。

## 探すべき情報（これ以外は不要）
- 上記業界の企業による28卒向けインターンシップの募集開始・エントリー締切
- 上記業界の企業による28卒向け本選考（新卒採用）のエントリー開始・締切
- 説明会・セミナーの応募締切（選考に直結するもの、28卒対象）

## 除外すべき情報
- 締切や応募期間が明記されていない一般ニュース・業績・トレンド記事
- すでに締切が過ぎているもの（今日の日付より前の締切）
- 28卒以外の学年向けの情報

## 具体的な締切日の特定について（最重要）
「エントリー受付中」「募集中」としか出てこず具体的な日付が無い場合、それだけで諦めて deadline を null にしないこと。
以下のように、より具体的なクエリで追加検索して日付を特定する努力をすること:
- 「{{企業名}} 28卒 インターン エントリー締切 いつまで」
- 「{{企業名}} 28卒 インターン 応募期間」
- 見つかった募集ページのURLに具体的な日付が書かれていそうであれば、そのページ自体の内容を検索結果から読み取る
それでも本当に日付が見つからない場合のみ deadline を null とし、summary に「締切日は公式サイトで要確認」と明記すること。

## 出力ルール
- 検索して実際に見つかった情報のみを使うこと。推測や一般論は書かないこと
- 締切・応募期間が具体的にわかるものを優先し、3〜8件に厳選する（同程度に興味深い情報が複数ある場合、日付が明確な方を優先する）
- 締切が近い順に並べること（deadlineがnullのものは末尾に）
- company_desc には「その企業が何をやっている会社か」を初見でもわかるように1文で簡潔にまとめること
- 出力は以下のJSON配列のみ（説明文・Markdownのコードブロック不要）:
[{{"company": "企業名", "company_desc": "その企業が何をしているかの簡潔な説明（1文）", "title": "募集内容（例：28卒本選考 エントリー締切）", "url": "https://...", "deadline": "YYYY-MM-DD（本当にわからなければnull）", "summary": "1〜2文の要約"}}]
- 該当する情報が無ければ空配列 [] を返す"""

    response = await _create_with_retry(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 15}],
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text" and b.text)
    try:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        return json.loads(match.group() if match else text)
    except Exception as e:
        logger.warning(f"[research_job_news] JSON解析失敗: {e} / text={text[:300]}")
        return []


async def analyze_job_email(subject: str, sender: str, body: str, career_goal: str, score: int) -> dict:
    """就活メールを1回のAPIコールで要約・ToDo抽出する。"""
    today = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    prompt = f"""以下の就活メールを分析してください。

今日の日付: {today}
キャリア目標: {career_goal or "未設定"}
送信者: {sender}
件名: {subject}
本文:
{body[:3000]}

以下のJSON形式で返してください（他のテキスト不要）:
{{
  "summary": "2〜3文での要約",
  "action": "推奨アクション（具体的に）",
  "deadline": "期限（なければnull）",
  "todos": [
    {{"title": "企業名 + アクション内容（例：NTT R&D 面接日程を返信する）", "due_date": "YYYY-MM-DDまたはnull"}}
  ]
}}

## todosの抽出ルール
- 返信・申し込み・提出・予約など具体的なアクションが必要なものだけ生成する
- 確認のみ・返信不要・期限も操作も不要なケース → "todos": []"""

    response = await _create_with_retry(
        model=CLAUDE_HAIKU_MODEL,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    text = _response_text(response)
    try:
        return _extract_json(text)
    except Exception:
        return {"summary": text[:200], "action": "内容を確認してください", "deadline": None, "todos": []}
