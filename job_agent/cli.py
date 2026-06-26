from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Optional

from .config import (
    append_history,
    ensure_dirs,
    load_cv_variants,
    safe_slug,
    write_json,
)
from .models import JobRecord


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- intake command ---
    intake_parser = subparsers.add_parser(
        "intake", help="Capture page state (a11y tree + screenshot) without filling"
    )
    intake_parser.add_argument("job_url")
    intake_parser.add_argument("--headless", action="store_true")

    # --- fill command ---
    fill_parser = subparsers.add_parser(
        "fill", help="Run the agentic loop to fill the form"
    )
    fill_parser.add_argument("job_url")
    fill_parser.add_argument("--headless", action="store_true")
    fill_parser.add_argument("--dry-run", action="store_true", help="Show LLM plan without executing")

    # --- bot command ---
    subparsers.add_parser("bot", help="Start Telegram bot (polling)")

    args = parser.parse_args(argv)
    ensure_dirs()

    if args.command == "intake":
        return asyncio.run(_run_intake(args.job_url, headed=not args.headless))

    if args.command == "fill":
        return asyncio.run(_run_fill(
            args.job_url, headed=not args.headless, dry_run=args.dry_run
        ))

    if args.command == "bot":
        return _run_bot()

    parser.error(f"Unknown command: {args.command}")
    return 2


async def _run_intake(job_url: str, *, headed: bool) -> int:
    """Capture page state for inspection."""
    from .page_capture import capture_page_state, setup_browser_page

    record = JobRecord(job_url=job_url)
    append_history(record.to_dict())

    p, browser, ctx, page = await setup_browser_page(job_url, headed=headed)
    try:
        state, elements = await capture_page_state(page)
        slug = safe_slug(job_url)
        output_path = __import__("pathlib").Path("data") / "screenshots" / f"{slug}-intake.json"
        from .page_capture import format_elements_for_llm
        write_json(output_path, {
            "url": state.url,
            "title": state.title,
            "screenshot": state.screenshot_path,
            "element_count": len(elements),
            "elements": format_elements_for_llm(elements),
        })
        print(json.dumps({
            "url": state.url,
            "title": state.title,
            "screenshot": state.screenshot_path,
            "snapshot_length": len(state.aria_snapshot),
        }, indent=2))
    finally:
        await ctx.close()
        await browser.close()
        await p.stop()

    record.status = "intake_captured"
    append_history(record.to_dict())
    return 0


async def _run_fill(job_url: str, *, headed: bool, dry_run: bool) -> int:
    """Run the agentic fill loop."""
    from .agent import run_agent

    record = JobRecord(job_url=job_url)
    append_history(record.to_dict())

    result = await run_agent(job_url, headed=headed, dry_run=dry_run)

    print(json.dumps({
        "status": result.status,
        "filled_count": result.filled_count,
        "error_count": result.error_count,
        "cv_variant": result.cv_variant,
    }, indent=2))

    append_history(result.to_dict())
    return 0


def _run_bot() -> int:
    """Start the Telegram bot."""
    import os
    from .telegram import _ensure_env, get_chat_id, save_chat_id

    _ensure_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        return 1

    # Import here to avoid dependency if not running bot
    try:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, MessageHandler, filters
        from telegram.request import HTTPXRequest
    except ImportError:
        print("Error: python-telegram-bot not installed. Run: pip install python-telegram-bot")
        return 1

    from .telegram import send_telegram

    async def start(update: Update, _context) -> None:
        await update.message.reply_text(
            "Send me a job posting URL and I'll fill the application for your review.\n"
            "/help — more info"
        )

    async def help_text(update: Update, _context) -> None:
        await update.message.reply_text(
            "Send any job URL and I'll:\n"
            "1. Open the page in a browser on my end\n"
            "2. Use AI to understand the form and fill it\n"
            "3. Leave it open for your review — I never click submit\n\n"
            "If I get stuck, I'll ask you via this chat."
        )

    async def handle_message(update: Update, _context) -> None:
        text = (update.message.text or "").strip()
        save_chat_id(update.effective_chat.id)

        import re
        match = re.search(r"https?://[^\s]+", text)
        if not match:
            await update.message.reply_text("No URL found. Please send a job posting URL.")
            return

        job_url = match.group(0)
        await update.message.reply_text(f"Got it. Starting:\n{job_url}")

        # Run agent in background
        asyncio.create_task(_run_agent_background(job_url, update.effective_chat.id))

    async def _run_agent_background(job_url: str, chat_id: int) -> None:
        try:
            from .agent import run_agent
            await run_agent(job_url, headed=False)
        except Exception as exc:
            await send_telegram(f"Agent failed: {exc}")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Starting Telegram bot (polling)...")
    async def _poll():
        async with app:
            await app.start()
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            await asyncio.Event().wait()

    asyncio.run(_poll())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
