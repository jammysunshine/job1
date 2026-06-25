from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .decision_engine import map_fields
from .handlers.generic_handler import GenericHandler
from .intake import _safe_slug, capture_page_evidence
from .llm import classify_intake
from .models import JobRecord, utc_now_iso
from .storage import EVIDENCE_DIR, ROOT, append_history, ensure_data_dirs, write_json
from .telegram_bot import run_bot


def main(argv: Optional[list[str]] = None) -> int:
    from .telegram_bot import load_env as _load_env
    _load_env()

    parser = argparse.ArgumentParser(prog="job-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    intake_parser = subparsers.add_parser("intake", help="Capture evidence and run LLM intake classification")
    intake_parser.add_argument("job_url")
    intake_parser.add_argument("--headless", action="store_true", help="Run browser headless")

    fill_parser = subparsers.add_parser("fill", help="Run the four-stage pipeline (Extract -> Interpret -> Map -> Fill)")
    fill_parser.add_argument("job_url")
    fill_parser.add_argument("--headless", action="store_true", help="Run browser headless")
    fill_parser.add_argument("--dry-run", action="store_true", help="Run Stages A+B+C only — no filling")

    subparsers.add_parser("bot", help="Start Telegram bot (polling)")

    args = parser.parse_args(argv)
    ensure_data_dirs()

    if args.command == "intake":
        return asyncio.run(_run_intake(args.job_url, headed=not args.headless))

    if args.command == "fill":
        return asyncio.run(_run_fill(args.job_url, headed=not args.headless, dry_run=args.dry_run))

    if args.command == "bot":
        return run_bot()

    parser.error(f"Unknown command: {args.command}")
    return 2


async def _run_intake(job_url: str, *, headed: bool) -> int:
    record = JobRecord(job_url=job_url)
    append_history(record.to_dict())

    evidence = await capture_page_evidence(job_url, headed=headed)
    llm_result = classify_intake(evidence)

    record.status = "intake_classified"
    record.updated_at = utc_now_iso()
    record.evidence_path = str(EVIDENCE_DIR / _evidence_name(evidence.final_url))
    record.screenshot_path = evidence.screenshot_path
    if "ats_type" in llm_result:
        record.ats_type = llm_result.get("ats_type")
        record.ats_confidence = llm_result.get("detection_confidence")

    result_path = EVIDENCE_DIR / f"{_evidence_name(evidence.final_url).removesuffix('.json')}-llm.json"
    write_json(result_path, llm_result)
    append_history(record.to_dict())

    print(json.dumps({
        "status": record.status,
        "evidence_path": record.evidence_path,
        "llm_result_path": str(result_path),
        "screenshot_path": record.screenshot_path,
        "ats_type": record.ats_type,
        "ats_confidence": record.ats_confidence,
    }, indent=2))
    return 0


async def _scrape_job_description(job_url: str) -> Optional[str]:
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)
            text = await page.evaluate("() => document.body.innerText")
            await browser.close()
            return text[:6000]
    except Exception:
        return None


async def _run_fill(job_url: str, *, headed: bool, dry_run: bool) -> int:
    record = JobRecord(job_url=job_url)
    append_history(record.to_dict())

    # ATS detection is informational only — never used for routing (v3 spec §4)
    evidence = await capture_page_evidence(job_url, headed=headed)
    llm_result = classify_intake(evidence)
    ats_type = llm_result.get("ats_type", "unknown")
    print(f"ATS detection (informational): {ats_type} (confidence: {llm_result.get('detection_confidence', 'N/A')})")
    record.ats_type = ats_type
    record.ats_confidence = llm_result.get("detection_confidence")

    handler = GenericHandler(headless=not headed)

    if dry_run:
        print("\nDry run — Stages A (Extract) + B (Interpret) + C (Map) only.")
        form_url, fields, screenshot_path = await handler.discover_fields(job_url)
        print(f"\nStage A: discovered {len(fields)} fields on {form_url}")
        print(f"Screenshot: {screenshot_path}")

        from .field_interpreter import interpret_fields
        jd_text = await _scrape_job_description(job_url)
        interpreted = interpret_fields(fields, page_text=jd_text)
        print(f"\nStage B (Field Interpreter) annotations:")
        print(json.dumps(interpreted, indent=2))

        job_context = {"job_url": job_url, "form_url": form_url, "mapping_scope": "current_page_only"}
        if jd_text:
            job_context["job_description"] = jd_text
        mapping = map_fields(fields, job_context=job_context)
        print(f"\nStage C (Mapper) output:")
        print(json.dumps(mapping, indent=2))

        record.status = "fields_mapped"
        record.updated_at = utc_now_iso()
        evidence_path = EVIDENCE_DIR / f"{_safe_slug(job_url)}-mapping.json"
        write_json(evidence_path, {"ats_type": ats_type, **mapping})
        record.evidence_path = str(evidence_path)
        append_history(record.to_dict())
        return 0

    print("\nFull pipeline — running Stages A->D with multi-step loop ...")

    from .handlers._shared import discover_chat_id as _discover_chat_id
    chat_path = ROOT / "data" / "telegram_chat_id.txt"
    if not chat_path.exists():
        cid = _discover_chat_id()
        if cid:
            print("Chat ID auto-discovered from Telegram! Notifications active.\n")
        else:
            print("\nMessage @Mohit_job_bot on Telegram (any text), then press Enter.")
            print("   Or press Ctrl+C to skip Telegram and use terminal input.")
            input("   Press Enter when ready... ")
            cid = _discover_chat_id()
            if not cid:
                print("   Still no chat ID. Proceeding without Telegram.\n")
            else:
                print("   Chat ID registered! Telegram notifications active.\n")

    print("Filling form...")
    from .decision_engine import load_cv_variants
    variants = load_cv_variants()
    cv_path = variants[0].get("file_path") if variants else None

    mapping: Dict[str, Any] = {"fields": [], "cv_variant": variants[0].get("name") if variants else None}
    result = await handler.fill_and_stop(job_url, mapping, cv_path=cv_path)

    print(json.dumps({
        "fill_status": result.status,
        "filled_fields": result.filled_fields,
        "errors": result.errors,
        "screenshot_path": result.screenshot_path,
    }, indent=2))

    record.status = result.status
    record.updated_at = utc_now_iso()
    append_history(record.to_dict())

    return 0


def _evidence_name(url: str) -> str:
    from .intake import _safe_slug
    return f"{_safe_slug(url)}.json"


if __name__ == "__main__":
    raise SystemExit(main())
