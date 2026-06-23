from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .decision_engine import map_fields
from .handlers import HANDLER_REGISTRY, get_handler
from .intake import _safe_slug, capture_page_evidence
from .llm import classify_intake
from .models import JobRecord, utc_now_iso
from .storage import EVIDENCE_DIR, append_history, ensure_data_dirs, write_json
from .telegram_bot import run_bot


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="job-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    intake_parser = subparsers.add_parser("intake", help="Capture evidence and run LLM intake classification")
    intake_parser.add_argument("job_url")
    intake_parser.add_argument("--headless", action="store_true", help="Run browser headless")

    fill_parser = subparsers.add_parser("fill", help="Discover fields, map via LLM, and fill form")
    fill_parser.add_argument("job_url")
    fill_parser.add_argument("--ats", help="Override ATS type (e.g. careers_page)")
    fill_parser.add_argument("--headless", action="store_true", help="Run browser headless")
    fill_parser.add_argument("--dry-run", action="store_true", help="Discover and map fields only, don't fill")

    subparsers.add_parser("bot", help="Start Telegram bot (polling)")

    args = parser.parse_args(argv)
    ensure_data_dirs()

    if args.command == "intake":
        return asyncio.run(_run_intake(args.job_url, headed=not args.headless))

    if args.command == "fill":
        return asyncio.run(_run_fill(args.job_url, ats=args.ats, headed=not args.headless, dry_run=args.dry_run))

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


async def _run_fill(job_url: str, *, ats: Optional[str], headed: bool, dry_run: bool) -> int:
    record = JobRecord(job_url=job_url)
    append_history(record.to_dict())

    if not ats:
        evidence = await capture_page_evidence(job_url, headed=headed)
        llm_result = classify_intake(evidence)
        ats = llm_result.get("ats_type", "unknown")
        print(f"Intake classified ATS: {ats} (confidence: {llm_result.get('detection_confidence', 'N/A')})")

    handler_cls = get_handler(ats)
    if not handler_cls:
        print(f"No handler registered for ATS: {ats}")
        print(f"Registered handlers: {list(HANDLER_REGISTRY.keys())}")
        return 1

    handler = handler_cls(headless=not headed)
    form_url, fields, screenshot_path = await handler.discover_fields(job_url)

    print(f"\nDiscovered {len(fields)} fields on {form_url}")
    print(f"Screenshot: {screenshot_path}\n")

    jd_text = await _scrape_job_description(job_url)
    job_context = {
        "job_url": job_url,
        "form_url": form_url,
    }
    if jd_text:
        job_context["job_description"] = jd_text

    mapping = map_fields(fields, job_context=job_context)

    print(json.dumps(mapping, indent=2))

    record.status = "fields_mapped"
    record.updated_at = utc_now_iso()
    evidence_path = EVIDENCE_DIR / f"{_safe_slug(job_url)}-mapping.json"
    write_json(evidence_path, mapping)
    record.evidence_path = str(evidence_path)
    record.ats_type = ats
    append_history(record.to_dict())

    if dry_run or not mapping.get("fields"):
        print("\nDry run — no fields filled.")
        return 0

    print("\nFilling form...")
    cv_path = None
    cv_variant = mapping.get("cv_variant")
    if cv_variant:
        from .decision_engine import load_cv_variants
        variants = load_cv_variants()
        for v in variants:
            if v.get("name") == cv_variant:
                cv_path = v.get("file_path")
                break

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
