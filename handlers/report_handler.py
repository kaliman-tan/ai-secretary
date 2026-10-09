import asyncio
import discord
import logging
import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo
from services import claude_service, db_service, calendar_service, notion_service, google_tasks_service
from config import DISCORD_REPORT_CHANNEL_ID, TIMEZONE, ANTHROPIC_CURRENT_CREDITS, ANTHROPIC_MONTHLY_USED, ANTHROPIC_DAILY_AVG

_SENT_DATE_FILE = os.path.join(tempfile.gettempdir(), "ai_secretary_report_sent.txt")
_send_lock = asyncio.Lock()
logger = logging.getLogger(__name__)
_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def is_sent_today() -> bool:
    try:
        tz = ZoneInfo(TIMEZONE)
        today = datetime.now(tz).strftime("%Y-%m-%d")
        with open(_SENT_DATE_FILE) as f:
            return f.read().strip() == today
    except FileNotFoundError:
        return False


def _mark_sent() -> None:
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    with open(_SENT_DATE_FILE, "w") as f:
        f.write(today)


async def _fetch_gcal_events() -> list[dict]:
    try:
        return await asyncio.to_thread(calendar_service.get_today_events, days_ahead=1)
    except Exception:
        return []


async def _fetch_notion_events() -> list[dict]:
    try:
        return await asyncio.to_thread(notion_service.get_upcoming_events, days_ahead=7)
    except Exception:
        return []


async def generate_and_post(bot: discord.Client, discord_summary: str = "", force: bool = False) -> None:
    async with _send_lock:
        if not force and is_sent_today():
            logger.info("[report] 本日の日次レポートは送信済みのためスキップ")
            return
        await _generate_and_post_inner(bot, discord_summary)


async def _generate_and_post_inner(bot: discord.Client, discord_summary: str = "") -> None:
    channel = bot.get_channel(DISCORD_REPORT_CHANNEL_ID)
    if channel is None:
        return

    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    weekday = _WEEKDAY_JA[now.weekday()]
    today = f"{now.year}年{now.month}月{now.day}日（{weekday}）"

    # データを並列取得
    emails, todos, gcal_events, notion_events = await asyncio.gather(
        asyncio.to_thread(db_service.get_recent_email_logs, 10),
        asyncio.to_thread(google_tasks_service.get_tasks),
        _fetch_gcal_events(),
        _fetch_notion_events(),
    )

    all_events = gcal_events + [
        e for e in notion_events if not any(g["title"] == e["title"] for g in gcal_events)
    ]

    report = await claude_service.generate_daily_report(
        emails=emails,
        todos=todos,
        calendar_events=all_events,
        discord_summary=discord_summary,
    )

    credit_section = _build_credit_section()
    header = f"# 📅 {today} 日次レポート\n\n"
    full_message = header + report + "\n\n" + credit_section
    for chunk in _split_message(full_message):
        await channel.send(chunk)
    _mark_sent()
    logger.info(f"[report] 日次レポート送信完了: {today}")


def _split_message(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _build_credit_section() -> str:
    days_remaining = (
        int(ANTHROPIC_CURRENT_CREDITS / ANTHROPIC_DAILY_AVG)
        if ANTHROPIC_DAILY_AVG > 0
        else 0
    )
    return (
        "---\n"
        "💰 **APIクレジット情報**\n"
        f"・残りクレジット：${ANTHROPIC_CURRENT_CREDITS:.2f}\n"
        f"・今月の使用額：${ANTHROPIC_MONTHLY_USED:.2f}\n"
        f"・使用ペース：1日あたり約${ANTHROPIC_DAILY_AVG:.2f}\n"
        f"・残り目安：約{days_remaining}日分\n"
        "詳細確認 → https://console.anthropic.com/settings/billing"
    )
