from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from .handlers._shared import (
    discover_chat_id as _discover_chat_id,
    load_chat_id,
    save_chat_id,
    send_telegram as send_message,
)

# backward-compat alias: everything should use _shared.send_telegram directly
send_telegram = send_message
from .models import JobRecord, utc_now_iso
from .storage import ROOT, append_history, ensure_data_dirs

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)

CHAT_ID_PATH = ROOT / "data" / "telegram_chat_id.txt"
LEARN_SIGNAL_PATH = ROOT / "data" / "learn_signal.json"
LEARN_DONE_PATH = ROOT / "data" / "learn_done.json"

_fill_in_progress = False
_fill_lock = asyncio.Lock()


def load_env() -> None:
    """Load .env into environment (called once at startup)."""
    from .handlers._shared import _ensure_env
    _ensure_env()


async def start(update: Update, _context) -> None:
    await update.message.reply_text(
        "Send me a job posting URL and I'll process it.\n"
        "/help — more info"
    )


async def help_text(update: Update, _context) -> None:
    await update.message.reply_text(
        "Send any job URL (LinkedIn, company career page, etc.) and I'll:\n"
        "1. Extract form fields from the page\n"
        "2. Interpret what each field means\n"
        "3. Map your profile data to each field\n"
        "4. Fill the form for your review\n\n"
        "I will never click submit — that's your job."
    )


async def status(update: Update, _context) -> None:
    global _fill_in_progress
    msg = "Bot is running."
    if _fill_in_progress:
        msg += "\nA fill is currently in progress."
    await update.message.reply_text(msg)


_bot_instance: Optional[Bot] = None


def _get_bot() -> Optional[Bot]:
    global _bot_instance
    if _bot_instance:
        return _bot_instance
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    _bot_instance = Bot(token=token, request=HTTPXRequest(connect_timeout=10, read_timeout=10))
    return _bot_instance


async def handle_message(update: Update, _context) -> None:
    global _fill_in_progress

    text = (update.message.text or "").strip()
    save_chat_id(update.effective_chat.id)

    lower = text.lower()
    if lower in ("save", "capture", "learn"):
        LEARN_SIGNAL_PATH.write_text(json.dumps({"command": lower, "chat_id": update.effective_chat.id}))
        await update.message.reply_text("Capturing form values... stand by.")
        for _ in range(30):
            await asyncio.sleep(1)
            if LEARN_DONE_PATH.exists():
                done = json.loads(LEARN_DONE_PATH.read_text())
                LEARN_DONE_PATH.unlink(missing_ok=True)
                await update.message.reply_text(done.get("message", "Saved! You can submit now."))
                return
        await update.message.reply_text("No fill process responded. Is a fill running?")
        return

    match = URL_RE.search(text)
    if not match:
        return

    job_url = match.group(0)
    ensure_data_dirs()
    record = JobRecord(job_url=job_url, user_note=text)
    append_history(record.to_dict())
    logger.info("Received job URL: %s", job_url)

    if _fill_in_progress:
        await update.message.reply_text(
            f"A fill is already in progress. I've noted:\n{job_url}\n\n"
            "Try again when the current one finishes."
        )
        return

    await update.message.reply_text(
        f"Got it. Starting the pipeline for:\n{job_url}\n\n"
        "I'll send updates as each stage completes."
    )

    async with _fill_lock:
        _fill_in_progress = True
    try:
        asyncio.create_task(_run_fill_background(job_url, update.effective_chat.id))
    except Exception as e:
        _fill_in_progress = False
        logger.error("Failed to start fill task: %s", e)
        await update.message.reply_text(f"Error starting fill: {e}")


async def _run_fill_background(job_url: str, chat_id: int) -> None:
    global _fill_in_progress
    try:
        await send_telegram(f"🔍 Capturing page evidence for:\n{job_url}")
        from .intake import capture_page_evidence
        from .llm import classify_intake
        evidence = await capture_page_evidence(job_url, headed=False)
        llm_result = classify_intake(evidence)
        ats_type = llm_result.get("ats_type", "unknown")
        await send_telegram(f"📋 ATS detected (informational): {ats_type}")

        from .handlers.generic_handler import GenericHandler
        handler = GenericHandler(headless=False)

        await send_telegram(
            f"⚙️ Running pipeline (Extract → Interpret → Map → Fill).\n"
            f"Browser will open on your Mac. You may need to solve a CAPTCHA or enter a PIN."
        )

        from .decision_engine import load_cv_variants
        variants = load_cv_variants()
        cv_path = variants[0].get("file_path") if variants else None
        mapping = {"fields": [], "cv_variant": variants[0].get("name") if variants else None}

        result = await handler.fill_and_stop(job_url, mapping, cv_path=cv_path)

        if result.errors:
            await send_telegram(
                f"⚠️ Fill completed with {len(result.errors)} issue(s):\n"
                + "\n".join(result.errors[:3])
                + "\n\nBrowser is open for your review. I will NOT submit."
            )
        else:
            await send_telegram(
                f"✅ All fields filled ({len(result.filled_fields)} fields).\n"
                f"Browser is open for your review. I will NOT submit."
            )

        record = JobRecord(job_url=job_url, status=result.status)
        append_history(record.to_dict())

    except Exception as e:
        logger.error("Fill pipeline failed: %s", e, exc_info=True)
        await send_telegram(f"❌ Pipeline failed: {e}")
    finally:
        _fill_in_progress = False


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

    async def _poll():
        async with app:
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await asyncio.Event().wait()

    asyncio.run(_poll())
    return 0


if __name__ == "__main__":
    raise SystemExit(run_bot())
