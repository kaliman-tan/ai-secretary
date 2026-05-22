import asyncio
import discord
from datetime import datetime
from services import supabase_service

_WEEKDAY_NAMES = ["月", "火", "水", "木", "金", "土", "日"]


async def post_todo_list(channel: discord.TextChannel) -> None:
    todos = await asyncio.to_thread(supabase_service.get_todos, False)
    if not todos:
        await channel.send("未完了のタスクはありません。")
        return

    lines = ["**📋 ToDoリスト（未完了）**\n"]
    for t in todos:
        priority_mark = {"high": "🔴", "normal": "🟡", "low": "🟢"}.get(t.get("priority", "normal"), "")
        lines.append(f"⬜ {priority_mark} `{t['id']}` {t['title']}")

    await channel.send("\n".join(lines))


async def handle_add(channel: discord.TextChannel, title: str, deadline_dt: datetime | None = None) -> None:
    due_date_str = deadline_dt.isoformat() if deadline_dt else None
    await asyncio.to_thread(supabase_service.add_todo, title, "normal", due_date_str)

    msg = f"✅ 「{title}」を追加したよ！"
    if deadline_dt:
        weekday = _WEEKDAY_NAMES[deadline_dt.weekday()]
        msg += f"\n📅 締め切り：{deadline_dt.month}月{deadline_dt.day}日（{weekday}）{deadline_dt.strftime('%H:%M')}"
        msg += "\n🔔 忘れないようにリマインドするね！"
    await channel.send(msg)


async def handle_complete(channel: discord.TextChannel, query: str) -> None:
    matched = await asyncio.to_thread(supabase_service.complete_todo_by_title, query)
    if matched:
        titles = ", ".join(f"**{t['title']}**" for t in matched)
        await channel.send(f"✅ 完了にしました: {titles}")
    else:
        try:
            todo_id = int(query)
            await asyncio.to_thread(supabase_service.complete_todo, todo_id)
            await channel.send(f"✅ ID {todo_id} のタスクを完了にしました。")
        except (ValueError, Exception):
            await channel.send(f"⚠️ 「{query}」に一致するタスクが見つかりませんでした。")


async def handle_delete(channel: discord.TextChannel, query: str) -> None:
    matched = await asyncio.to_thread(supabase_service.delete_todo_by_title, query)
    if matched:
        titles = ", ".join(f"**{t['title']}**" for t in matched)
        await channel.send(f"🗑️ 削除しました: {titles}")
    else:
        try:
            todo_id = int(query)
            await asyncio.to_thread(supabase_service.delete_todo, todo_id)
            await channel.send(f"🗑️ ID {todo_id} のタスクを削除しました。")
        except (ValueError, Exception):
            await channel.send(f"⚠️ 「{query}」に一致するタスクが見つかりませんでした。")
