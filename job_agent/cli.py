from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .intake import capture_page_evidence
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

    subparsers.add_parser("bot", help="Start Telegram bot (polling)")

    args = parser.parse_args(argv)
    ensure_data_dirs()

    if args.command == "intake":
        return asyncio.run(_run_intake(args.job_url, headed=not args.headless))

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


def _evidence_name(url: str) -> str:
    from .intake import _safe_slug

    return f"{_safe_slug(url)}.json"


if __name__ == "__main__":
    raise SystemExit(main())
