import asyncio
import discord
from datetime import datetime
from zoneinfo import ZoneInfo
from services import claude_service, supabase_service
from config import DISCORD_JOB_RESEARCH_CHANNEL_ID, TIMEZONE

_TZ = ZoneInfo(TIMEZONE)


async def run_batch(bot: discord.Client) -> None:
    """志望業界の新着ニュースをリサーチしてDiscordに投稿する。"""
    if DISCORD_JOB_RESEARCH_CHANNEL_ID == 0:
        return
    channel = bot.get_channel(DISCORD_JOB_RESEARCH_CHANNEL_ID)
    if channel is None:
        return

    now = datetime.now(_TZ)
    today_str = f"{now.month}月{now.day}日"

    profile = supabase_service.get_user_profile()
    priorities = profile.get("priorities", "")

    try:
        items = await claude_service.research_job_news(priorities)
    except Exception as e:
        print(f"[job_research] リサーチエラー: {e}")
        return

    new_items = [it for it in items if it.get("url") and not await asyncio.to_thread(
        supabase_service.is_research_logged, it["url"]
    )]

    if not new_items:
        await channel.send(f"🔎 **就活リサーチ**（{today_str}分）\n今日は新着の関連情報はなかったよ！")
        return

    for it in new_items:
        await asyncio.to_thread(
            supabase_service.save_research_log,
            it["url"], it.get("title", ""), it.get("summary", ""),
        )

    lines = [
        f"・**{it.get('title', '(タイトルなし)')}**\n  {it.get('summary', '')}\n  <{it['url']}>"
        for it in new_items
    ]
    await channel.send(
        f"🔎 **就活リサーチ**（{today_str}分） — 新着{len(new_items)}件\n\n" + "\n\n".join(lines)
    )
