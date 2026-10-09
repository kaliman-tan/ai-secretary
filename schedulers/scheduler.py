import asyncio
import logging
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo
from config import (
    DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE, TIMEZONE,
    ANTHROPIC_CURRENT_CREDITS, ANTHROPIC_DAILY_AVG, DISCORD_CREDIT_CHANNEL_ID,
)
from handlers import report_handler, job_email_handler, job_research_handler
from services import db_service

logger = logging.getLogger(__name__)


def setup(bot: discord.Client) -> AsyncIOScheduler:
    tz = ZoneInfo(TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    # 就活メールバッチ（深夜0時）— misfire_grace_time で数秒のズレでもスキップされないように
    scheduler.add_job(
        _run_job_email_batch,
        CronTrigger(hour=0, minute=0, timezone=tz),
        args=[bot],
        id="job_email_batch",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # 日次レポート（毎朝）— misfire_grace_time で起動遅延でも確実実行
    scheduler.add_job(
        _run_daily_report,
        CronTrigger(hour=DAILY_REPORT_HOUR, minute=DAILY_REPORT_MINUTE, timezone=tz),
        args=[bot],
        id="daily_report",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # 就活リサーチ（深夜0時）— 自動実行は停止中（2026-09-01〜）。
    # チャットで「就活リサーチ実行して」と話しかければ check_job_research ツール経由で手動実行は引き続き可能。
    # 再開する場合は以下のコメントを外す。
    # scheduler.add_job(
    #     _run_job_research,
    #     CronTrigger(hour=0, minute=0, timezone=tz),
    #     args=[bot],
    #     id="job_research_batch",
    #     misfire_grace_time=3600,
    #     coalesce=True,
    # )

    # DBバックアップ（毎日 3:30）— data/backups/ に14世代まで保持
    scheduler.add_job(
        _run_db_backup,
        CronTrigger(hour=3, minute=30, timezone=tz),
        id="db_backup",
        misfire_grace_time=3600,
        coalesce=True,
    )

    # クレジット残高チェック（1時間ごと）
    scheduler.add_job(
        _run_credit_check,
        "interval",
        hours=1,
        args=[bot],
        id="credit_check",
    )

    return scheduler


async def _run_db_backup() -> None:
    path = await asyncio.to_thread(db_service.backup)
    logger.info(f"[db_backup] スナップショット作成: {path.name}")


async def _run_job_email_batch(bot: discord.Client) -> None:
    await job_email_handler.run_batch(bot)


async def _run_job_research(bot: discord.Client) -> None:
    await job_research_handler.run_batch(bot)


async def _run_daily_report(bot: discord.Client) -> None:
    discord_summary = await _collect_discord_summary(bot)
    await report_handler.generate_and_post(bot, discord_summary)


async def _run_credit_check(bot: discord.Client) -> None:
    if DISCORD_CREDIT_CHANNEL_ID == 0:
        return
    channel = bot.get_channel(DISCORD_CREDIT_CHANNEL_ID)
    if channel is None:
        return

    credits = ANTHROPIC_CURRENT_CREDITS
    days_remaining = int(credits / ANTHROPIC_DAILY_AVG) if ANTHROPIC_DAILY_AVG > 0 else 0
    billing_url = "https://console.anthropic.com/settings/billing"

    if credits <= 1.00:
        await channel.send(
            f"🚨 **クレジット残高が危険水域だよ！！**\n"
            f"残り：${credits:.2f}\n"
            f"このままだとAPIが止まっちゃう！すぐチャージして！\n"
            f"→ {billing_url}"
        )
    elif credits <= 3.00:
        await channel.send(
            f"⚠️ **クレジット残高が少なくなってきたよ！**\n"
            f"残り：${credits:.2f}（約{days_remaining}日分）\n"
            f"早めにチャージしてね！\n"
            f"→ {billing_url}"
        )


async def _collect_discord_summary(bot: discord.Client) -> str:
    """全チャンネルの直近メッセージを収集してサマリーを作る（簡易版）"""
    from config import DISCORD_GUILD_ID
    from services import claude_service

    guild = bot.get_guild(DISCORD_GUILD_ID)
    if guild is None:
        return ""

    collected = []
    for channel in guild.text_channels[:5]:
        try:
            async for msg in channel.history(limit=20):
                if not msg.author.bot:
                    collected.append(f"[{channel.name}] {msg.author.display_name}: {msg.content}")
        except discord.Forbidden:
            continue

    if not collected:
        return ""

    text = "\n".join(collected[-50:])
    return await claude_service.chat(f"以下のDiscordログを3〜5文で要約してください:\n{text}")
