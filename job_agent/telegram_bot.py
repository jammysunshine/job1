from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .models import JobRecord, utc_now_iso
from .storage import ROOT, append_history, ensure_data_dirs

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def start(update: Update, _context) -> None:
    await update.message.reply_text(
        "Send me a job posting URL and I'll process it.\n"
        "/help — more info"
    )


async def help_text(update: Update, _context) -> None:
    await update.message.reply_text(
        "Send any job URL (LinkedIn, company career page, etc.) and I'll:\n"
        "1. Capture the page with Playwright\n"
        "2. Detect the ATS vendor\n"
        "3. Ask you for anything I can't infer\n"
        "4. Fill the form for your review\n\n"
        "I will never click submit — that's your job."
    )


async def status(update: Update, _context) -> None:
    await update.message.reply_text("Bot is running.")


async def handle_message(update: Update, _context) -> None:
    text = (update.message.text or "").strip()
    match = URL_RE.search(text)
    if not match:
        await update.message.reply_text(
            "I didn't find a URL in that message. Send me a job posting link."
        )
        return

    job_url = match.group(0)
    ensure_data_dirs()
    record = JobRecord(job_url=job_url, user_note=text)
    append_history(record.to_dict())

    logger.info("Received job URL: %s", job_url)
    await update.message.reply_text(
        f"Got it. I've queued:\n{job_url}\n\n"
        "I'll let you know once I've processed it."
    )


async def error_handler(update: object, context) -> None:
    logger.error("Exception while handling update: %s", context.error)


def run_bot() -> int:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        return 1

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_text))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    print("Starting Telegram bot (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_bot())
