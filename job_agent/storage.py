from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
EVIDENCE_DIR = DATA_DIR / "evidence"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
HISTORY_PATH = DATA_DIR / "history.jsonl"
APPLICATIONS_PATH = DATA_DIR / "applications.json"


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def log_submission(job_url: str, ats_type: Optional[str], company: Optional[str] = None, role: Optional[str] = None) -> int:
    ensure_data_dirs()
    apps = {"submissions": [], "total_count": 0}
    if APPLICATIONS_PATH.exists():
        try:
            apps = json.loads(APPLICATIONS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    entry = {
        "date": datetime.now(timezone.utc).isoformat(),
        "job_url": job_url,
        "ats_type": ats_type,
        "company": company,
        "role": role,
        "status": "submitted",
    }
    apps["submissions"].append(entry)
    apps["total_count"] = len(apps["submissions"])
    APPLICATIONS_PATH.write_text(json.dumps(apps, indent=2) + "\n")
    return apps["total_count"]


def get_submission_count() -> int:
    if APPLICATIONS_PATH.exists():
        try:
            return json.loads(APPLICATIONS_PATH.read_text()).get("total_count", 0)
        except (json.JSONDecodeError, OSError):
            return 0
    return 0


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def write_json(path: Path, payload: Any) -> Path:
    ensure_data_dirs()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(payload), indent=2) + "\n", encoding="utf-8")
    return path


def append_history(record: Dict[str, Any]) -> None:
    ensure_data_dirs()
    with HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")

