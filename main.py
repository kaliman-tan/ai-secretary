import asyncio
import logging
import discord
from datetime import datetime
from zoneinfo import ZoneInfo
from discord.ext import commands
from config import DISCORD_BOT_TOKEN, DISCORD_GUILD_ID, DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE, TIMEZONE
from handlers import message_handler
from schedulers import scheduler as scheduler_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("ai_secretary.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
_scheduler = None


@bot.event
async def on_ready():
    global _scheduler
    logger.info(f"[AI秘書] ログイン完了: {bot.user}")

    _scheduler = scheduler_module.setup(bot)
    _scheduler.start()
    logger.info("[AI秘書] スケジューラー起動")

    # 起動時刻が日次レポート時刻を過ぎていて未送信なら補完送信
    from handlers import report_handler
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    report_due = now.hour > DAILY_REPORT_HOUR or (
        now.hour == DAILY_REPORT_HOUR and now.minute >= DAILY_REPORT_MINUTE
    )
    if report_due and not report_handler.is_sent_today():
        logger.warning(f"[AI秘書] 起動が{DAILY_REPORT_HOUR:02d}:{DAILY_REPORT_MINUTE:02d}以降のため日次レポートを補完送信します")
        asyncio.create_task(_deferred_startup_report())


async def _deferred_startup_report() -> None:
    await asyncio.sleep(5)  # botの接続安定待ち
    from handlers import report_handler
    await report_handler.generate_and_post(bot)


@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if message.guild and message.guild.id != DISCORD_GUILD_ID:
        return

    await message_handler.handle(message, bot)
    await bot.process_commands(message)


@bot.command(name="report")
async def manual_report(ctx):
    """手動で日次レポートを生成"""
    await ctx.send("📊 レポート生成中...")
    from handlers import report_handler
    await report_handler.generate_and_post(bot)


@bot.command(name="todo")
async def show_todo(ctx):
    """ToDoリスト表示"""
    from handlers import todo_handler
    from config import DISCORD_TODO_CHANNEL_ID
    channel = bot.get_channel(DISCORD_TODO_CHANNEL_ID)
    await todo_handler.post_todo_list(channel or ctx.channel)


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
