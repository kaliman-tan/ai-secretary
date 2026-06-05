import asyncio
import re
import discord
from datetime import date, datetime
from zoneinfo import ZoneInfo
from services import gmail_service, claude_service, supabase_service, google_tasks_service
from config import DISCORD_EMAIL_CHANNEL_ID, TIMEZONE, GMAIL_JOB_LABEL

# channel_id -> list of todo candidates waiting for user selection
_pending_todos: dict[int, list[dict]] = {}

_TZ = ZoneInfo(TIMEZONE)
_SCORE_EMOJI = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐", 4: "⭐⭐⭐⭐", 5: "⭐⭐⭐⭐⭐"}
_DIVIDER = "━━━━━━━━━━━━━━━━━━"
_DEFAULT_THRESHOLD = 4


def _get_threshold() -> int:
    profile = supabase_service.get_user_profile()
    return int((profile.get("preferences") or {}).get("email_score_threshold", _DEFAULT_THRESHOLD))


async def run_batch(bot: discord.Client) -> None:
    """就活メールバッチ処理を実行する（4ステップ）。"""
    channel = bot.get_channel(DISCORD_EMAIL_CHANNEL_ID)
    if channel is None:
        return

    now = datetime.now(_TZ)
    today_str = f"{now.month}月{now.day}日"
    profile = supabase_service.get_user_profile()
    career_goal = profile.get("career_goal", "")

    # Step 1: 件名・送信者のみ取得（本文なし）
    emails = await asyncio.to_thread(
        gmail_service.get_emails_metadata,
        GMAIL_JOB_LABEL,
        24,
        500,
    )
    if not emails:
        await channel.send(f"📧 **就活メール**（{today_str}分）\n新着メールはなかったよ！")
        return

    # Step 2: スコアリング（10件ずつバッチ処理）
    scores = await claude_service.score_job_emails(emails, career_goal)

    dist: dict[int, int] = {i: 0 for i in range(1, 6)}
    for s in scores:
        dist[s.get("score", 1)] = dist.get(s.get("score", 1), 0) + 1
    print(
        f"[job_email] スコア分布 — "
        + ", ".join(f"スコア{k}: {v}件" for k, v in sorted(dist.items(), reverse=True))
        + f" / 合計{len(emails)}件"
    )

    threshold = _get_threshold()
    high_priority = sorted(
        [
            {**emails[s["index"]], "score": s["score"], "reason": s.get("reason", "")}
            for s in scores
            if s.get("score", 0) >= threshold
        ],
        key=lambda x: x["score"],
        reverse=True,
    )

    if not high_priority:
        await channel.send(
            f"📧 **就活メール重要ピックアップ**（{today_str}分）\n"
            f"スコア{threshold}以上のメールはなかったよ！（全{len(emails)}件）"
        )
        return

    # Step 3 & 3.5: 本文取得・要約・ログ保存 → ToDo候補収集（自動追加はしない）
    results = []
    todo_candidates: list[dict] = []
    for email in high_priority:
        already = await asyncio.to_thread(supabase_service.is_email_logged, email["id"])
        if not already:
            body = await asyncio.to_thread(gmail_service.get_email_body, email["id"])

            # 要約・ToDo抽出を1回のAPIコールで実行
            try:
                result = await claude_service.analyze_job_email(
                    subject=email["subject"],
                    sender=email["sender"],
                    body=body,
                    career_goal=career_goal,
                    score=email["score"],
                )
                summary_data = result
                email_todos = result.get("todos", [])
            except Exception as e:
                print(f"[job_email] メール処理エラー ({email['subject']}): {e}")
                summary_data = {"summary": "", "action": "内容を確認してください", "deadline": None}
                email_todos = []

            await asyncio.to_thread(
                supabase_service.save_email_log,
                email["id"], email["sender"], email["subject"],
                "高" if email["score"] == 5 else "中",
                summary_data.get("summary", ""),
            )

            for todo in email_todos:
                todo_candidates.append({
                    "title": todo["title"],
                    "due_date": todo.get("due_date"),
                    "source_id": email["id"],
                    "score": email["score"],
                })

        results.append(email)

    # Step 4: Discordに投稿
    count_5 = sum(1 for r in results if r["score"] == 5)
    count_4 = sum(1 for r in results if r["score"] == 4)

    header = (
        f"📧 **就活メール重要ピックアップ**（{today_str}分）\n"
        f"スコア{threshold}以上 {len(results)}件 / 全{len(emails)}件中\n"
        f"{_DIVIDER}"
    )
    footer = f"{_DIVIDER}\n要確認 {count_5}件 · 要チェック {count_4}件"

    blocks = []
    for r in results:
        stars = _SCORE_EMOJI.get(r["score"], "⭐" * r["score"])
        reason = r.get("reason", "").strip()
        sender_display = r["sender"].split("<")[0].strip() or r["sender"]
        blocks.append(f"{stars} `{r['subject']}`\n└ {sender_display} · {reason}")

    await _send_chunked(channel, header, blocks, footer)

    # Step 5: ToDo候補を確認メッセージとして表示
    if todo_candidates:
        _pending_todos[channel.id] = todo_candidates
        lines = [
            f"**{i+1}.** {t['title']}{_fmt_due(t.get('due_date'))}"
            for i, t in enumerate(todo_candidates)
        ]
        await channel.send(
            f"📋 **ToDo候補 {len(todo_candidates)}件** — 追加したいものを番号で教えてね！\n"
            f"（例：`1,3` / `全部` / `なし`）\n\n"
            + "\n".join(lines)
        )


async def handle_todo_confirmation(channel: discord.TextChannel, content: str) -> None:
    """ToDo候補の選択を処理する。"""
    todos = _pending_todos.get(channel.id, [])
    if not todos:
        return

    lower = content.strip().lower()

    if any(w in lower for w in ["全部", "全て", "すべて", "all", "全"]):
        selected_indices = list(range(len(todos)))
    elif any(w in lower for w in ["なし", "いらない", "スキップ", "no", "キャンセル", "skip"]):
        del _pending_todos[channel.id]
        await channel.send("わかった！ToDoの追加はスキップするね！")
        return
    else:
        nums = re.findall(r'\d+', content)
        selected_indices = [int(n) - 1 for n in nums if 1 <= int(n) <= len(todos)]
        if not selected_indices:
            lines = [f"**{i+1}.** {t['title']}{_fmt_due(t.get('due_date'))}" for i, t in enumerate(todos)]
            await channel.send(
                "番号で教えてね！（例：`1,3` / `全部` / `なし`）\n\n" + "\n".join(lines)
            )
            return

    del _pending_todos[channel.id]
    added = []
    for i in selected_indices:
        todo = todos[i]
        saved = await asyncio.to_thread(
            supabase_service.add_email_todo,
            todo["title"], todo.get("due_date"), todo["source_id"], todo["score"],
        )
        if saved:
            due_iso = (todo["due_date"] + "T00:00:00+09:00") if todo.get("due_date") else None
            await asyncio.to_thread(
                google_tasks_service.create_task,
                todo["title"], due_iso, None,
            )
            added.append(todo)

    if added:
        lines = [f"・{t['title']}{_fmt_due(t.get('due_date'))}" for t in added]
        await channel.send(f"✅ **{len(added)}件のToDoを追加したよ！**\n" + "\n".join(lines))
    else:
        await channel.send("追加完了！（すでに登録済みのものはスキップしたよ）")


def _fmt_due(due_date_str: str | None) -> str:
    if not due_date_str:
        return ""
    try:
        d = date.fromisoformat(due_date_str)
        return f"（期限：{d.month}/{d.day}）"
    except ValueError:
        return f"（期限：{due_date_str}）"


async def _send_chunked(
    channel: discord.TextChannel,
    header: str,
    blocks: list[str],
    footer: str,
    limit: int = 1900,
) -> None:
    current = header
    for block in blocks:
        candidate = current + "\n\n" + block
        if len(candidate) > limit:
            await channel.send(current)
            current = block
        else:
            current = candidate

    if current:
        await channel.send(current + "\n" + footer)
