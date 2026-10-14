import asyncio
import discord
import json
import logging
import re
import time
import anthropic
from datetime import datetime, timedelta, date as date_t
from zoneinfo import ZoneInfo
from services import claude_service, db_service, calendar_service, notion_service, google_tasks_service
from handlers import todo_handler
from config import TIMEZONE

logger = logging.getLogger(__name__)

_TZ = ZoneInfo(TIMEZONE)
_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]

# channel_id -> list[{"role": ..., "content": ...}]
_conversation_history: dict[int, list[dict]] = {}
_MAX_HISTORY = 20
_history_loaded: set[int] = set()  # DBから読み込み済みのchannel_id

# channel_id -> [{title, start_dt, end_dt, created}] : 確認待ちイベント（複数可。「OK」でまとめて登録）
_pending_events: dict[int, list[dict]] = {}
_PENDING_EVENT_TTL = 3600  # 秒。これより古い確認待ちは無効にする（後日の無関係な「OK」で登録されないように）

# channel_id -> {task_id, title} : 実施日時の回答待ちタスク
_pending_task_dates: dict[int, dict] = {}

# ツール結果をClaudeに返して最終応答を生成するツール群
_QUERY_TOOLS = {"list_todos", "list_events"}

# ユーザーの返事を待つツール。実行後はループを止め、Claude に追加の文を書かせない
_AWAIT_REPLY_TOOLS = {"add_event"}

# 操作系ツールのあと、Claude に「追加で伝えることは無い」を表させる印（送信せずに捨てる）
_NOTHING_TO_ADD = "（なし）"

# 確認待ち・回答待ちへの返事として扱うのは「その語だけ」の返事に限る（記号・絵文字・空白は無視）。
# 部分一致にすると「これも登録してほしい」や「参加登録をお願いします」を含む長文まで確定扱いになる。
_CONFIRM_REPLIES = {
    "ok", "okです", "okay", "yes", "はい", "うん", "いいよ", "いいね", "よし", "おけ", "おk", "おっけ", "おっけー",
    "お願い", "おねがい", "お願いします", "登録して", "登録", "確定", "確定して", "それで", "それでok",
    "それでお願い", "大丈夫", "だいじょうぶ",
}
_CANCEL_REPLIES = {
    "no", "いや", "いいえ", "キャンセル", "キャンセルして", "やめ", "やめて", "やめる", "取り消し", "取り消して",
    "なし", "いらない",
}
_SKIP_DATE_REPLIES = {
    "あとで", "後で", "あとでいい", "あとでやる", "あとで決める", "まだ", "まだ決めてない", "決めてない",
    "未定", "わからない", "わかんない", "skip", "スキップ", "なし", "パス",
}
# 日時の修正・回答として扱う短い返事の目印（「16時に変えて」「明日の朝」など）
_DATETIME_HINT = re.compile(
    r"[0-9０-９]|今日|明日|明後日|あさって|今週|来週|再来週|[月火水木金土日]曜|午前|午後|朝|昼|夕方|夜|時|分|半"
)
_SHORT_REPLY_MAX = 30


def _norm_reply(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _looks_like_datetime_reply(text: str) -> bool:
    return len(text.strip()) <= _SHORT_REPLY_MAX and bool(_DATETIME_HINT.search(text))


async def _load_history_if_needed(channel_id: int) -> None:
    if channel_id in _history_loaded:
        return
    _history_loaded.add(channel_id)
    try:
        messages = await asyncio.to_thread(db_service.get_conversation_history, channel_id)
        if messages:
            _conversation_history[channel_id] = _sanitize_history(messages)
    except Exception:
        logger.warning(f"[history] DBからの履歴読み込みに失敗: channel={channel_id}")


async def _save_history(channel_id: int) -> None:
    try:
        messages = _conversation_history.get(channel_id, [])
        await asyncio.to_thread(db_service.save_conversation_history, channel_id, messages)
    except Exception:
        logger.warning(f"[history] DBへの履歴保存に失敗: channel={channel_id}")


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

    # 初回アクセス時にDBから履歴を復元（確認待ちへの返事も履歴に残すので最初に読む）
    await _load_history_if_needed(channel_id)

    # 確認待ちイベントへの返事（「OK」「キャンセル」「16時に変えて」等）。該当しなければ通常の会話へ
    reply = await _handle_event_confirmation(channel, content)
    if reply is not None:
        await _record_exchange(channel_id, content, reply)
        return

    # ToDo候補の選択待ちがあれば処理（番号選択に見えないメッセージは通常会話にフォールバック）
    from handlers import job_email_handler, job_research_handler
    if channel_id in job_email_handler._pending_todos:
        if await job_email_handler.handle_todo_confirmation(channel, content):
            return
    if channel_id in job_research_handler._pending_todos:
        if await job_research_handler.handle_todo_confirmation(channel, content):
            return

    # タスク実施日時への返事。日時でない話題なら質問を取り下げて通常の会話へ
    reply = await _handle_task_date_response(channel, content)
    if reply is not None:
        await _record_exchange(channel_id, content, reply)
        return

    # Tool Use で意図判定と応答を1コールで解決
    history = _get_history(channel_id)
    result = await claude_service.chat_with_tools(content, history)

    _push_history(channel_id, "user", content)

    _MAX_TOOL_CALLS = 5
    tool_call_count = 0
    final_text_sent = False
    last_tool_summary: str | None = None
    executed: set[str] = set()

    while result["type"] == "tool_use" and tool_call_count < _MAX_TOOL_CALLS:
        signature = f"{result['name']}:{json.dumps(result['input'], ensure_ascii=False, sort_keys=True)}"
        if tool_call_count > 0 and (result["name"] in _QUERY_TOOLS or signature in executed):
            # 操作のあとに続く一覧表示（頼まれていない確認）や、同じ操作の繰り返し（重複追加）は実行しない
            break
        executed.add(signature)
        tool_call_count += 1
        # アシスタントのtool_useブロックを正規APIフォーマットで記録
        _push_message(channel_id, {"role": "assistant", "content": result["assistant_content"]})
        summary = await _dispatch_tool(channel, bot, result["name"], result["input"])
        result_text = summary or "実行完了"
        last_tool_summary = result_text
        tool_result_text = result_text
        if result["name"] not in _QUERY_TOOLS | _AWAIT_REPLY_TOOLS:
            # 操作系ツールはツール側で報告を送信済み。Claude が同じ報告を繰り返して二重送信にならないようにする
            tool_result_text += (
                "\n（この結果はユーザーに送信済み。同じ報告を繰り返さないこと。"
                "ユーザーの依頼のうち未実行のものがあればそれだけ続けて実行する。"
                "ユーザーの質問や相談にまだ答えていなければ答える。"
                f"それ以外に伝えることが無ければ「{_NOTHING_TO_ADD}」とだけ返す）"
            )
        # tool_resultをuserメッセージとして記録
        _push_message(channel_id, {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": result["tool_use_id"], "content": tool_result_text}],
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
        elif result["name"] in _AWAIT_REPLY_TOOLS:
            # 確認メッセージは送信済みで、ユーザーの返事を待つ。Claude に状況説明を書かせない
            _push_history(channel_id, "assistant", result_text)
            final_text_sent = True
            break
        else:
            # アクション系ツール: 残りの依頼があれば続けて実行させる
            result = await claude_service.chat_with_tools(None, _get_history(channel_id))

    if not final_text_sent:
        text = result.get("text", "") if result["type"] == "text" else ""
        if text and _norm_reply(text) != _norm_reply(_NOTHING_TO_ADD):
            _push_history(channel_id, "assistant", text)
            await channel.send(text)
        elif last_tool_summary:
            # Claude が追加で書くことが無かった場合も、履歴は assistant(要約) で閉じておく（送信はしない）
            _push_history(channel_id, "assistant", last_tool_summary)

    # 履歴をDBに保存（awaitで競合状態を防ぐ）
    await _save_history(channel_id)


async def _record_exchange(channel_id: int, user_text: str, bot_text: str) -> None:
    """ツールを通らないやり取り（確認待ちへの返事など）も履歴に残す。後から「さっきの」で参照できるように。"""
    _push_history(channel_id, "user", user_text)
    _push_history(channel_id, "assistant", bot_text)
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
        # 送った確認メッセージそのものを結果にする（次のターンで Claude がユーザーと同じ文面を見られる）
        return await _handle_add_event_with_confirm(channel, inputs)

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
            db_service.add_custom_rule,
            inputs["rule_type"], inputs["target"], inputs["action"],
        )
        await channel.send(
            f"✅ カスタムルールを追加したよ！ **{inputs['target']}** → {inputs['action']}"
        )
        return f"メールルールを追加した: {inputs['target']} → {inputs['action']}"

    elif name == "add_contact":
        await asyncio.to_thread(
            db_service.upsert_contact,
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
        topic = inputs.get("topic")
        label = f"「{topic}」について" if topic else ""
        await channel.send(f"🔎 就活リサーチ{label}を実行中...ちょっと待ってね！")
        await run_batch(bot, channel=channel, topic=topic)
        return f"就活リサーチを実行した{'（対象: ' + topic + '）' if topic else ''}"

    else:
        logger.warning(f"[dispatch] 未知のツール: {name}")
        await channel.send("ごめん、そのリクエストは処理できなかった…もう一度試してみてね！")
        return None


# ---------------------------------------------------------------------------
# Event registration with confirmation
# ---------------------------------------------------------------------------

def _fmt_event_range(p: dict) -> str:
    start, end = p["start_dt"], p["end_dt"]
    weekday = _WEEKDAY_NAMES[start.weekday()]
    return f"{start.strftime('%m/%d')}（{weekday}）{start.strftime('%H:%M')}〜{end.strftime('%H:%M')}"


def _live_pending_events(channel_id: int) -> list[dict]:
    """期限切れを除いた確認待ちイベントを返す（全部切れていればエントリごと消す）。"""
    now = time.monotonic()
    live = [p for p in _pending_events.get(channel_id, []) if now - p["created"] < _PENDING_EVENT_TTL]
    if live:
        _pending_events[channel_id] = live
    else:
        _pending_events.pop(channel_id, None)
    return live


async def _handle_add_event_with_confirm(channel: discord.TextChannel, inputs: dict) -> str:
    """確認待ちに追加して確認メッセージを送り、その文面を返す。

    既に確認待ちがあれば上書きせず並べて一緒に確認する。同じタイトルは時刻の修正とみなして置き換える。
    """
    title = inputs.get("title") or "新しい予定"
    start_dt = _parse_iso(inputs.get("start_iso"))
    if start_dt is None:
        now_jst = datetime.now(_TZ)
        start_dt = (now_jst + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    end_dt = _parse_iso(inputs.get("end_iso"))
    if end_dt is None or end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)

    pending = [p for p in _live_pending_events(channel.id) if p["title"] != title]
    pending.append({"title": title, "start_dt": start_dt, "end_dt": end_dt, "created": time.monotonic()})
    _pending_events[channel.id] = pending

    if len(pending) == 1:
        msg = (
            f"📅 {_fmt_event_range(pending[0])} に **{title}** を登録するね！\n"
            f"OKなら「OK」、日時が違ったら修正内容を教えてっ！"
        )
    else:
        lines = "\n".join(f"・{_fmt_event_range(p)} **{p['title']}**" for p in pending)
        msg = (
            f"📅 確認待ちの予定が{len(pending)}件あるよ！まとめて登録するね！\n{lines}\n"
            f"「OK」で全部登録、「キャンセル」で全部やめるよ！"
            f"片方だけにしたいときは「キャンセル」してから、登録したい方をもう一度送ってね！"
        )
    await channel.send(msg)
    return msg


async def _handle_event_confirmation(channel: discord.TextChannel, content: str) -> str | None:
    """確認待ちへの返事なら処理して送った文面を返す。返事ではない（別の話題・別の依頼）なら None。"""
    pending = _live_pending_events(channel.id)
    if not pending:
        return None

    reply = _norm_reply(content)

    if reply in _CONFIRM_REPLIES:
        del _pending_events[channel.id]
        texts = [await _execute_event_registration(channel, p) for p in pending]
        return "\n".join(texts)

    if reply in _CANCEL_REPLIES:
        del _pending_events[channel.id]
        msg = "わかった！キャンセルしたよ！また教えてね！"
        await channel.send(msg)
        return msg

    # 時刻の修正は、対象が1件に決まるときだけ受け付ける（複数あるとどれの修正か分からない）
    if len(pending) == 1 and _looks_like_datetime_reply(content):
        p = pending[0]
        try:
            new_dt = await claude_service.parse_datetime_natural(content, datetime.now(_TZ), base_dt=p["start_dt"])
        except Exception:
            msg = "うーん、うまく読み取れなかった…\n「OK」で確定、「キャンセル」でやめられるよ！"
            await channel.send(msg)
            return msg
        duration = p["end_dt"] - p["start_dt"]
        p["start_dt"], p["end_dt"], p["created"] = new_dt, new_dt + duration, time.monotonic()
        msg = f"📅 {_fmt_event_range(p)} に修正したよ！\nこれでOK？「OK」で確定！"
        await channel.send(msg)
        return msg

    return None


async def _handle_task_date_response(channel: discord.TextChannel, content: str) -> str | None:
    """「いつやる予定？」への返事なら処理して送った文面を返す。返事ではないなら質問を取り下げて None。"""
    pending = _pending_task_dates.get(channel.id)
    if not pending:
        return None

    if _norm_reply(content) in _SKIP_DATE_REPLIES:
        del _pending_task_dates[channel.id]
        msg = "わかった！日付なしで登録しておくね！"
        await channel.send(msg)
        return msg

    if not _looks_like_datetime_reply(content):
        # 日時の返事ではない = 話題が変わった。ToDo は日付なしのまま、通常の会話として扱う
        del _pending_task_dates[channel.id]
        return None

    try:
        dt = await claude_service.parse_datetime_natural(content, datetime.now(_TZ))
        await asyncio.to_thread(google_tasks_service.update_task_due, pending["task_id"], dt.isoformat())
    except Exception:
        msg = (
            "うーん、日時をうまく読み取れなかった…\n"
            "「明日15時」「来週月曜の午前中」みたいに教えてくれると嬉しいな！\n"
            "「あとで」と言えば日付なしのままにできるよ！"
        )
        await channel.send(msg)
        return msg

    del _pending_task_dates[channel.id]
    weekday = _WEEKDAY_NAMES[dt.weekday()]
    msg = f"📅 「{pending['title']}」を {dt.month}/{dt.day}（{weekday}）{dt.strftime('%H:%M')} にやる予定で設定したよ！"
    await channel.send(msg)
    return msg


async def _execute_event_registration(channel: discord.TextChannel, p: dict) -> str:
    """Google Calendar（+ Notion）に登録し、送った文面を返す。"""
    try:
        gcal = await asyncio.to_thread(calendar_service.create_event, p["title"], p["start_dt"], p["end_dt"])
        msg = f"✅ 登録したよ！\n📅 **{p['title']}** — {_fmt_event_range(p)}\n{gcal.get('link', '')}"
    except Exception as e:
        msg = f"⚠️ Google Calendar登録エラー（{p['title']}）: {e}"
    await channel.send(msg)

    try:
        await asyncio.to_thread(notion_service.create_event, p["title"], p["start_dt"].date().isoformat())
    except Exception:
        pass
    return msg


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

    await asyncio.to_thread(db_service.merge_user_profile, updates)

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
