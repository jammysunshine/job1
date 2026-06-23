from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
EVIDENCE_DIR = DATA_DIR / "evidence"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
HISTORY_PATH = DATA_DIR / "history.jsonl"


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


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

