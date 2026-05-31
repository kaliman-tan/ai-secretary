import asyncio
import discord
from datetime import datetime
from zoneinfo import ZoneInfo
from services import google_tasks_service

_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]
_TZ = ZoneInfo("Asia/Tokyo")


async def post_todo_list(channel: discord.TextChannel) -> None:
    todos = await asyncio.to_thread(google_tasks_service.get_tasks)
    if not todos:
        await channel.send("未完了のタスクはありません。")
        return

    lines = ["**📋 ToDoリスト（未完了）**\n"]
    for t in todos:
        due_str = _fmt_due(t.get("due"))
        lines.append(f"⬜ {t['title']}{due_str}")

    await channel.send("\n".join(lines))


async def handle_add(
    channel: discord.TextChannel,
    title: str,
    due_dt: datetime | None = None,
    scheduled_dt: datetime | None = None,
) -> dict:
    notes = None
    if scheduled_dt:
        notes = f"scheduled:{scheduled_dt.isoformat()}"

    due_iso = (scheduled_dt or due_dt).isoformat() if (scheduled_dt or due_dt) else None
    task = await asyncio.to_thread(google_tasks_service.create_task, title, due_iso, notes)

    msg = f"✅ 「{title}」を追加したよ！"
    if scheduled_dt:
        weekday = _WEEKDAY_NAMES[scheduled_dt.weekday()]
        msg += f"\n📅 実施予定：{scheduled_dt.month}月{scheduled_dt.day}日（{weekday}）{scheduled_dt.strftime('%H:%M')}"
    elif due_dt:
        weekday = _WEEKDAY_NAMES[due_dt.weekday()]
        msg += f"\n⏰ 期限：{due_dt.month}月{due_dt.day}日（{weekday}）{due_dt.strftime('%H:%M')}"
    else:
        msg += "\nいつやる予定？日時を教えてくれたら設定するね！「あとで」でスキップもできるよ！"
    await channel.send(msg)
    return task


async def handle_complete(channel: discord.TextChannel, query: str) -> None:
    matched = await asyncio.to_thread(google_tasks_service.search_tasks, query)
    if matched:
        for t in matched:
            await asyncio.to_thread(google_tasks_service.complete_task, t["id"])
        titles = ", ".join(f"**{t['title']}**" for t in matched)
        await channel.send(f"✅ 完了にしました: {titles}")
    else:
        await channel.send(f"⚠️ 「{query}」に一致するタスクが見つかりませんでした。")


async def handle_delete(channel: discord.TextChannel, query: str) -> None:
    matched = await asyncio.to_thread(google_tasks_service.search_tasks, query)
    if matched:
        for t in matched:
            await asyncio.to_thread(google_tasks_service.delete_task, t["id"])
        titles = ", ".join(f"**{t['title']}**" for t in matched)
        await channel.send(f"🗑️ 削除しました: {titles}")
    else:
        await channel.send(f"⚠️ 「{query}」に一致するタスクが見つかりませんでした。")


def _fmt_due(due_str: str | None) -> str:
    if not due_str:
        return ""
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(due_str.replace("Z", "+00:00")).astimezone(_TZ)
        return f"（{dt.month}/{dt.day}）"
    except Exception:
        return ""
