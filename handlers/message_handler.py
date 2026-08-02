import asyncio
import discord
import logging
import anthropic
from datetime import datetime, timedelta, date as date_t
from zoneinfo import ZoneInfo
from services import claude_service, supabase_service, calendar_service, notion_service, google_tasks_service
from handlers import todo_handler
from config import TIMEZONE

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(TIMEZONE)
_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

# channel_id -> list[{"role": ..., "content": ...}]
_conversation_history: dict[int, list[dict]] = {}
_MAX_HISTORY = 20
_history_loaded: set[int] = set()  # Supabaseから読み込み済みのchannel_id

# channel_id -> {title, start_dt, end_dt} : 確認待ちイベント
_pending_events: dict[int, dict] = {}

# channel_id -> {task_id, title} : 実施日時の回答待ちタスク
_pending_task_dates: dict[int, dict] = {}

# ツール結果をClaudeに返して最終応答を生成するツール群
_QUERY_TOOLS = {"list_todos", "list_events"}

_CONFIRM_WORDS = {"ok", "はい", "いいよ", "うん", "よし", "お願い", "登録して", "確定", "yes", "おk", "おけ", "おっけ", "おっけー"}
_CANCEL_WORDS = {"キャンセル", "やめ", "いや", "no", "取り消し", "やめて"}


async def _load_history_if_needed(channel_id: int) -> None:
    if channel_id in _history_loaded:
        return
    _history_loaded.add(channel_id)
    try:
        messages = await asyncio.to_thread(supabase_service.get_conversation_history, channel_id)
        if messages:
            _conversation_history[channel_id] = _sanitize_history(messages)
    except Exception:
        logger.warning(f"[history] Supabaseからの履歴読み込みに失敗: channel={channel_id}")


async def _save_history(channel_id: int) -> None:
    try:
        messages = _conversation_history.get(channel_id, [])
        await asyncio.to_thread(supabase_service.save_conversation_history, channel_id, messages)
    except Exception:
        logger.warning(f"[history] Supabaseへの履歴保存に失敗: channel={channel_id}")


def _push_message(channel_id: int, message: dict) -> None:
    history = _conversation_history.setdefault(channel_id, [])
    history.append(message)
    if len(history) > _MAX_HISTORY:
        _conversation_history[channel_id] = history[-_MAX_HISTORY:]


def _push_history(channel_id: int, role: str, content: str) -> None:
    _push_message(channel_id, {"role": role, "content": content})


def _sanitize_history(messages: list[dict]) -> list[dict]:
    """tool_useに対応するtool_resultが存在しない・不完全なペアを除去する。"""
    def _has_type(content: list, type_name: str) -> bool:
        return any(isinstance(b, dict) and b.get("type") == type_name for b in content)

    def _tool_use_ids(content: list) -> set:
        return {b["id"] for b in content if isinstance(b, dict) and b.get("type") == "tool_use"}

    def _tool_result_ids(content: list) -> set:
        return {b.get("tool_use_id") for b in content if isinstance(b, dict) and b.get("type") == "tool_result"}

    result = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if (msg.get("role") == "assistant"
                and isinstance(msg.get("content"), list)
                and _has_type(msg["content"], "tool_use")):
            next_msg = messages[i + 1] if i + 1 < len(messages) else None
            has_result = (
                next_msg is not None
                and isinstance(next_msg.get("content"), list)
                and _has_type(next_msg["content"], "tool_result")
            )
            all_matched = has_result and _tool_use_ids(msg["content"]).issubset(
                _tool_result_ids(next_msg["content"])
            )
            if all_matched:
                result.append(msg)
                result.append(next_msg)
                i += 2
            else:
                # 不完全なペアごとスキップ
                i += 2 if has_result else 1
        else:
            result.append(msg)
            i += 1
    return result


def _get_history(channel_id: int) -> list[dict]:
    history = _conversation_history.get(channel_id, [])[-12:]
    history = _sanitize_history(history)
    # 必ずユーザーテキストメッセージから始める（ツールサイクル途中からのスライスを防ぐ）
    for i, msg in enumerate(history):
        if msg.get("role") == "user" and isinstance(msg.get("content"), str):
            return history[i:]
    return []


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def handle(message: discord.Message, bot: discord.Client) -> None:
    if message.author.bot:
        return
    from services.claude_service import _is_overloaded
    try:
        await _handle_inner(message, bot)
    except Exception as e:
        if _is_overloaded(e):
            await message.channel.send(
                "うーん、今ちょっとAPIが混んでるみたい💦\n"
                "少し待ってからもう一度送ってみてね！"
            )
            return
        logger.exception(f"[handle] 予期しないエラー: channel={message.channel.id} content={message.content[:80]!r}")
        await message.channel.send(
            "ごめん、ちょっとエラーが起きちゃった😅\n"
            "もう一度試してみてね！"
        )


async def _handle_inner(message: discord.Message, bot: discord.Client) -> None:
    content = message.content.strip()
    channel = message.channel
    channel_id = channel.id

    # 確認待ちイベントがあれば最優先で処理
    if channel_id in _pending_events:
        await _handle_event_confirmation(channel, content)
        return

    # ToDo候補の選択待ちがあれば処理
    from handlers import job_email_handler, job_research_handler
    if channel_id in job_email_handler._pending_todos:
        await job_email_handler.handle_todo_confirmation(channel, content)
        return
    if channel_id in job_research_handler._pending_todos:
        await job_research_handler.handle_todo_confirmation(channel, content)
        return

    # タスク実施日時の回答待ちがあれば処理
    if channel_id in _pending_task_dates:
        await _handle_task_date_response(channel, content)
        return

    # 初回アクセス時にSupabaseから履歴を復元
    await _load_history_if_needed(channel_id)

    # Tool Use で意図判定と応答を1コールで解決
    history = _get_history(channel_id)
    result = await claude_service.chat_with_tools(content, history)

    _push_history(channel_id, "user", content)

    _MAX_TOOL_CALLS = 5
    tool_call_count = 0
    final_text_sent = False

    while result["type"] == "tool_use" and tool_call_count < _MAX_TOOL_CALLS:
        tool_call_count += 1
        # アシスタントのtool_useブロックを正規APIフォーマットで記録
        _push_message(channel_id, {"role": "assistant", "content": result["assistant_content"]})
        summary = await _dispatch_tool(channel, bot, result["name"], result["input"])
        result_text = summary or "実行完了"
        # tool_resultをuserメッセージとして記録
        _push_message(channel_id, {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": result["tool_use_id"], "content": result_text}],
        })
        if result["name"] in _QUERY_TOOLS and result["input"].get("user_question"):
            # user_questionがある場合のみClaudeで最終応答を生成（分析・アドバイスが必要）
            final_text = await claude_service.generate_final_response(_get_history(channel_id))
            text_to_send = final_text or result_text
            await channel.send(text_to_send)
            _push_history(channel_id, "assistant", text_to_send)
            final_text_sent = True
            break
        elif result["name"] in _QUERY_TOOLS:
            # user_questionなし: ツール結果をそのまま送信
            await channel.send(result_text)
            _push_history(channel_id, "assistant", result_text)
            final_text_sent = True
            break
        else:
            # アクション系ツール: 次のアクションをClaudeに判断させる
            result = await claude_service.chat_with_tools(None, _get_history(channel_id))

    if not final_text_sent:
        if result["type"] == "text" and result["text"]:
            _push_history(channel_id, "assistant", result["text"])
            await channel.send(result["text"])

    # 履歴をSupabaseに保存（awaitで競合状態を防ぐ）
    await _save_history(channel_id)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

async def _dispatch_tool(
    channel: discord.TextChannel,
    bot: discord.Client,
    name: str,
    inputs: dict,
) -> str | None:
    """ツールを実行し、次ターンの文脈として使える簡潔なサマリー文字列を返す。"""

    if name == "add_todo":
        due_dt = _parse_iso(inputs.get("due_iso"))
        scheduled_dt = _parse_iso(inputs.get("scheduled_iso"))
        task = await todo_handler.handle_add(channel, inputs["title"], due_dt, scheduled_dt)
        if not due_dt and not scheduled_dt and task:
            _pending_task_dates[channel.id] = {"task_id": task["id"], "title": inputs["title"]}
        suffix = f"（予定: {inputs['scheduled_iso']}）" if scheduled_dt else (f"（期限: {inputs['due_iso']}）" if due_dt else "")
        return f"ToDoを追加した: 「{inputs['title']}」{suffix}"

    elif name == "complete_todo":
        queries = inputs.get("queries") or [inputs.get("query", "")]
        for q in queries:
            await todo_handler.handle_complete(channel, q)
        labels = "・".join(f"「{q}」" for q in queries)
        return f"ToDoを完了にした: {labels}"

    elif name == "delete_todo":
        queries = inputs.get("queries") or [inputs.get("query", "")]
        for q in queries:
            await todo_handler.handle_delete(channel, q)
        labels = "・".join(f"「{q}」" for q in queries)
        return f"ToDoを削除した: {labels}"

    elif name == "list_todos":
        todos = await asyncio.to_thread(google_tasks_service.get_tasks)
        if todos:
            items = "\n".join(
                f"{i+1}. {t['title']}{(' (' + t['due'][:10] + ')') if t.get('due') else ''}"
                for i, t in enumerate(todos)
            )
            return f"未完了ToDoリスト（{len(todos)}件）:\n{items}"
        else:
            return "未完了のタスクはありません。"

    elif name == "add_event":
        await _handle_add_event_with_confirm(channel, inputs)
        return f"予定登録の確認待ち: 「{inputs.get('title')}」 {inputs.get('start_iso', '')}"

    elif name == "list_events":
        try:
            date_iso = inputs.get("date_iso")
            days = int(inputs.get("days") or 1)
            start_date = date_t.fromisoformat(date_iso) if date_iso else None

            events = await asyncio.to_thread(
                calendar_service.get_today_events,
                days_ahead=days,
                start_date=start_date,
            )

            if start_date and days == 1:
                weekday = _WEEKDAY_NAMES[start_date.weekday()]
                label = f"{start_date.month}/{start_date.day}（{weekday}）"
            elif days > 1:
                label = f"今後{days}日"
            else:
                label = "今日"

            if events:
                lines = "\n".join(
                    f"- {_fmt_event_time(e['start'], start_date)} {e['title']}"
                    for e in events
                )
                return f"{label}の予定（{len(events)}件）:\n{lines}"
            else:
                return f"{label}の予定はありません。"
        except Exception as e:
            logger.error(f"[list_events] エラー: {e}")
            return f"カレンダー取得エラー: {e}"

    elif name == "add_rule":
        await asyncio.to_thread(
            supabase_service.add_custom_rule,
            inputs["rule_type"], inputs["target"], inputs["action"],
        )
        await channel.send(
            f"✅ カスタムルールを追加したよ！ **{inputs['target']}** → {inputs['action']}"
        )
        return f"メールルールを追加した: {inputs['target']} → {inputs['action']}"

    elif name == "add_contact":
        await asyncio.to_thread(
            supabase_service.upsert_contact,
            email=inputs["email"], name=inputs["name"],
        )
        await channel.send(f"✅ 連絡先を登録したよ！ **{inputs['name']}** ({inputs['email']})")
        return f"連絡先を登録した: {inputs['name']} ({inputs['email']})"

    elif name == "update_profile":
        await _handle_profile_update(channel, inputs)
        saved = [f"{k}={v}" for k, v in inputs.items() if v]
        return f"プロフィールを更新した: {', '.join(saved)}"

    elif name == "check_job_email":
        from handlers.job_email_handler import run_batch
        await channel.send("📧 就活メールバッチを実行中...ちょっと待ってね！")
        await run_batch(bot)
        return "就活メールバッチを実行した"

    elif name == "check_job_research":
        from handlers.job_research_handler import run_batch
        await channel.send("🔎 就活リサーチを実行中...ちょっと待ってね！")
        await run_batch(bot)
        return "就活リサーチを実行した"

    else:
        logger.warning(f"[dispatch] 未知のツール: {name}")
        await channel.send("ごめん、そのリクエストは処理できなかった…もう一度試してみてね！")
        return None


# ---------------------------------------------------------------------------
# Event registration with confirmation
# ---------------------------------------------------------------------------

async def _handle_add_event_with_confirm(channel: discord.TextChannel, inputs: dict) -> None:
    title = inputs.get("title") or "新しい予定"
    start_iso = inputs.get("start_iso")

    if start_iso:
        start_dt = datetime.fromisoformat(start_iso)
    else:
        now_jst = datetime.now(_TZ)
        start_dt = (now_jst + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    end_dt = start_dt + timedelta(hours=1)
    weekday = _WEEKDAY_NAMES[start_dt.weekday()]

    _pending_events[channel.id] = {"title": title, "start_dt": start_dt, "end_dt": end_dt}

    await channel.send(
        f"📅 {start_dt.strftime('%m/%d')}（{weekday}）{start_dt.strftime('%H:%M')} に "
        f"**{title}** を登録するね！\n"
        f"OKなら「OK」、日時が違ったら修正内容を教えてっ！"
    )


async def _handle_event_confirmation(channel: discord.TextChannel, content: str) -> None:
    pending = _pending_events[channel.id]
    lower = content.lower().strip()

    if any(w in lower for w in _CONFIRM_WORDS):
        del _pending_events[channel.id]
        await _execute_event_registration(channel, pending["title"], pending["start_dt"], pending["end_dt"])
        return

    if any(w in content for w in _CANCEL_WORDS):
        del _pending_events[channel.id]
        await channel.send("わかった！キャンセルしたよ！また教えてね！")
        return

    # 修正として再解釈（parse_datetime_natural は確認フロー修正時のみ使用）
    now_jst = datetime.now(_TZ)
    try:
        new_dt = await claude_service.parse_datetime_natural(content, now_jst)
        pending["start_dt"] = new_dt
        pending["end_dt"] = new_dt + timedelta(hours=1)
        _pending_events[channel.id] = pending
        weekday = _WEEKDAY_NAMES[new_dt.weekday()]
        await channel.send(
            f"📅 {new_dt.strftime('%m/%d')}（{weekday}）{new_dt.strftime('%H:%M')} に修正したよ！\n"
            f"これでOK？「OK」で確定！"
        )
    except Exception:
        await channel.send(
            "うーん、うまく読み取れなかった…\n"
            "「OK」で確定、「キャンセル」でやめられるよ！"
        )


async def _handle_task_date_response(channel: discord.TextChannel, content: str) -> None:
    pending = _pending_task_dates[channel.id]
    skip_words = {"あとで", "まだ", "決めてない", "未定", "わからない", "skip", "なし", "パス", "後で"}
    if any(w in content for w in skip_words):
        del _pending_task_dates[channel.id]
        await channel.send("わかった！日付なしで登録しておくね！")
        return

    now_jst = datetime.now(_TZ)
    try:
        dt = await claude_service.parse_datetime_natural(content, now_jst)
        await asyncio.to_thread(google_tasks_service.update_task_due, pending["task_id"], dt.isoformat())
        del _pending_task_dates[channel.id]
        weekday = _WEEKDAY_NAMES[dt.weekday()]
        await channel.send(
            f"📅 「{pending['title']}」を {dt.month}/{dt.day}（{weekday}）{dt.strftime('%H:%M')} にやる予定で設定したよ！"
        )
    except Exception:
        await channel.send(
            "うーん、日時をうまく読み取れなかった…\n"
            "「明日15時」「来週月曜の午前中」みたいに教えてくれると嬉しいな！\n"
            "「あとで」と言えば日付なしのままにできるよ！"
        )


async def _execute_event_registration(
    channel: discord.TextChannel, title: str, start_dt: datetime, end_dt: datetime
) -> None:
    weekday = _WEEKDAY_NAMES[start_dt.weekday()]
    try:
        gcal = await asyncio.to_thread(calendar_service.create_event, title, start_dt, end_dt)
        await channel.send(
            f"✅ 登録したよ！\n"
            f"📅 **{title}** — {start_dt.strftime('%m/%d')}（{weekday}）{start_dt.strftime('%H:%M')}\n"
            f"{gcal.get('link', '')}"
        )
    except Exception as e:
        await channel.send(f"⚠️ Google Calendar登録エラー: {e}")

    try:
        await asyncio.to_thread(notion_service.create_event, title, start_dt.date().isoformat())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Profile update
# ---------------------------------------------------------------------------

async def _handle_profile_update(channel: discord.TextChannel, fields: dict) -> None:
    updates: dict = {
        "name": fields.get("name"),
        "role": fields.get("role"),
        "hobbies": fields.get("hobbies"),
        "traits": fields.get("traits"),
        "notes": fields.get("notes"),
        "priorities": fields.get("priorities"),
    }

    threshold = fields.get("email_score_threshold")
    if threshold is not None:
        updates["preferences"] = {"email_score_threshold": int(threshold)}

    await asyncio.to_thread(supabase_service.merge_user_profile, updates)

    saved_keys = [k for k, v in {
        "名前": fields.get("name"),
        "職業": fields.get("role"),
        "趣味": fields.get("hobbies"),
        "特性": fields.get("traits"),
        "志望業界・優先事項": fields.get("priorities"),
    }.items() if v]

    if threshold is not None:
        await channel.send(
            f"✅ メール重要度フィルターを **スコア{threshold}以上** に変更したよ！\n"
            f"次回のバッチからこの閾値で絞り込むね！"
        )
    elif fields.get("priorities"):
        await channel.send(
            f"✅ 志望業界・優先事項を更新したよ！\n"
            f"📌 {fields['priorities']}\n"
            f"次回のメールバッチからこの内容でスコアリングするね！"
        )
    elif saved_keys:
        items = "・".join(saved_keys)
        await channel.send(f"えへへ、わかった！{items}、ちゃんと覚えておくね！これからも何でも教えてっ！")
    else:
        await channel.send("うーん、情報をうまく読み取れなかったかも…もう少し詳しく教えてくれると嬉しいな！")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return None


def _fmt_event_time(iso_str: str, ref_date: date_t | None = None) -> str:
    """ISO8601の日時文字列を人が読みやすい形式に変換する。"""
    try:
        dt = datetime.fromisoformat(iso_str)
        if ref_date is None or dt.date() == ref_date:
            return dt.strftime("%H:%M")
        weekday = _WEEKDAY_NAMES[dt.weekday()]
        return f"{dt.month}/{dt.day}({weekday}) {dt.strftime('%H:%M')}"
    except Exception:
        # 日付のみ（終日イベント）の場合
        return iso_str
