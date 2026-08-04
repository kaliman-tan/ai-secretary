import asyncio
import re
import discord
from datetime import date, datetime
from zoneinfo import ZoneInfo
from services import claude_service, supabase_service, google_tasks_service
from config import DISCORD_JOB_RESEARCH_CHANNEL_ID, TIMEZONE

_TZ = ZoneInfo(TIMEZONE)

# 締切がこの日数以内に迫っていて、まだToDo化されていないものは再リマインドする
_REMINDER_WINDOW_DAYS = 3

# channel_id -> list of todo candidates waiting for user selection
_pending_todos: dict[int, list[dict]] = {}


async def run_batch(
    bot: discord.Client,
    channel: discord.TextChannel | None = None,
    topic: str | None = None,
) -> None:
    """インターン・選考募集/締切情報をリサーチしてDiscordに投稿する。

    channel を省略すると就活リサーチチャンネルに投稿する（毎日0時のバッチ用）。
    topic を指定すると、プロフィールの志望業界の代わりにそのトピックで検索する
    （チャットから「〇〇業界を調べて」等と指示された場合用）。
    """
    if channel is None:
        if DISCORD_JOB_RESEARCH_CHANNEL_ID == 0:
            return
        channel = bot.get_channel(DISCORD_JOB_RESEARCH_CHANNEL_ID)
        if channel is None:
            return

    now = datetime.now(_TZ)
    today_str = f"{now.month}月{now.day}日"

    if topic:
        target_hint = topic
    else:
        profile = await asyncio.to_thread(supabase_service.get_user_profile)
        target_hint = profile.get("priorities", "")

    try:
        items = await claude_service.research_job_news(target_hint)
    except Exception as e:
        print(f"[job_research] リサーチエラー: {e}")
        return

    new_items = []
    reminder_items = []
    for it in items:
        url = it.get("url")
        if not url:
            continue

        logged = await asyncio.to_thread(supabase_service.is_research_logged, url)
        if not logged:
            new_items.append(it)
            continue

        # 既出だが、ToDo化されておらず締切が近いものはリマインド対象にする
        already_added = await asyncio.to_thread(supabase_service.has_research_todo, url)
        if already_added:
            continue
        days_left = _days_until(it.get("deadline"))
        if days_left is not None and 0 <= days_left <= _REMINDER_WINDOW_DAYS:
            reminder_items.append(it)

    if not new_items and not reminder_items:
        await channel.send(f"🔎 **就活リサーチ**（{today_str}分）\n今日は新着の募集・締切情報はなかったよ！")
        return

    for it in new_items:
        await asyncio.to_thread(
            supabase_service.save_research_log,
            it["url"], _fmt_item_title(it), it.get("summary", ""),
        )

    def _block(it: dict, tag: str) -> str:
        return (
            f"{tag} **{it.get('company', '')}** — {it.get('company_desc', '')}\n"
            f"  {_fmt_item_title(it)}{_fmt_due(it.get('deadline'))}\n"
            f"  {it.get('summary', '')}\n"
            f"  <{it['url']}>"
        )

    blocks = [_block(it, "🆕") for it in new_items] + [_block(it, "⏰締切間近！") for it in reminder_items]
    header_parts = []
    if new_items:
        header_parts.append(f"新着{len(new_items)}件")
    if reminder_items:
        header_parts.append(f"締切間近リマインド{len(reminder_items)}件")

    await channel.send(
        f"🔎 **就活リサーチ**（{today_str}分） — {' / '.join(header_parts)}\n\n" + "\n\n".join(blocks)
    )

    # ToDo候補として提示（自動追加はしない）
    all_items = new_items + reminder_items
    _pending_todos[channel.id] = all_items
    lines = [
        f"**{i+1}.** {_fmt_item_title(it)}{_fmt_due(it.get('deadline'))}"
        for i, it in enumerate(all_items)
    ]
    await channel.send(
        f"📋 **ToDo候補 {len(all_items)}件** — 締切をToDoに追加したいものを番号で教えてね！\n"
        f"（例：`1,3` / `全部` / `なし`）\n\n"
        + "\n".join(lines)
    )


async def handle_todo_confirmation(channel: discord.TextChannel, content: str) -> bool:
    """ToDo候補の選択を処理する。選択として処理できた場合はTrue、
    番号選択でも全部/なしでもなさそうなら何もせずFalseを返す（呼び出し側で通常会話にフォールバックさせる）。"""
    items = _pending_todos.get(channel.id, [])
    if not items:
        return False

    lower = content.strip().lower()

    if any(w in lower for w in ["全部", "全て", "すべて", "all", "全"]):
        selected_indices = list(range(len(items)))
    elif any(w in lower for w in ["なし", "いらない", "スキップ", "no", "キャンセル", "skip"]):
        del _pending_todos[channel.id]
        await channel.send("わかった！ToDoの追加はスキップするね！")
        return True
    elif not re.search(r'\d', content):
        # 数字も選択キーワードも含まない = 番号選択への返信ではない
        return False
    else:
        nums = re.findall(r'\d+', content)
        selected_indices = [int(n) - 1 for n in nums if 1 <= int(n) <= len(items)]
        if not selected_indices:
            lines = [f"**{i+1}.** {_fmt_item_title(it)}{_fmt_due(it.get('deadline'))}" for i, it in enumerate(items)]
            await channel.send(
                "番号で教えてね！（例：`1,3` / `全部` / `なし`）\n\n" + "\n".join(lines)
            )
            return True

    del _pending_todos[channel.id]
    added = []
    for i in selected_indices:
        it = items[i]
        due_iso = (it["deadline"] + "T23:59:00+09:00") if it.get("deadline") else None
        saved = await asyncio.to_thread(
            supabase_service.add_research_todo,
            _fmt_item_title(it), it.get("deadline"), it["url"], "high" if it.get("deadline") else "normal",
        )
        if saved:
            await asyncio.to_thread(
                google_tasks_service.create_task,
                _fmt_item_title(it), due_iso, None,
            )
            added.append(it)

    if added:
        lines = [f"・{_fmt_item_title(it)}{_fmt_due(it.get('deadline'))}" for it in added]
        await channel.send(f"✅ **{len(added)}件のToDoを追加したよ！**\n" + "\n".join(lines))
    else:
        await channel.send("追加完了！（すでに登録済みのものはスキップしたよ）")
    return True


def _fmt_item_title(it: dict) -> str:
    company = it.get("company", "").strip()
    title = it.get("title", "").strip()
    if company and title:
        return f"{company} {title}"
    return title or company or "(タイトルなし)"


def _fmt_due(deadline_str: str | None) -> str:
    if not deadline_str:
        return ""
    try:
        d = date.fromisoformat(deadline_str)
        return f"（締切：{d.month}/{d.day}）"
    except ValueError:
        return f"（締切：{deadline_str}）"


def _days_until(deadline_str: str | None) -> int | None:
    if not deadline_str:
        return None
    try:
        d = date.fromisoformat(deadline_str)
        return (d - datetime.now(_TZ).date()).days
    except ValueError:
        return None
