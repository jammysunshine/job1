from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
EVIDENCE_DIR = DATA_DIR / "evidence"
HISTORY_PATH = DATA_DIR / "history.jsonl"
LEARNED_PATH = DATA_DIR / "learned_answers.json"
CHAT_ID_PATH = DATA_DIR / "telegram_chat_id.txt"


def ensure_dirs() -> None:
    for d in [DATA_DIR, SCREENSHOT_DIR, EVIDENCE_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> Dict[str, Any]:
    path = CONFIG_DIR / "profile.yaml"
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def load_cv_variants() -> List[Dict[str, Any]]:
    path = CONFIG_DIR / "cv_variants.yaml"
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}
            return data.get("cv_variants", [])
    return []


def load_learned_answers() -> List[Dict[str, Any]]:
    if LEARNED_PATH.exists():
        with open(LEARNED_PATH) as f:
            data = json.load(f) or {}
            return data.get("answers", [])
    return []


def load_cover_letter_template() -> Optional[str]:
    path = CONFIG_DIR / "cover_letter_template.txt"
    if path.exists():
        return path.read_text().strip()
    return None


def safe_slug(value: str) -> str:
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return slug[:120] or "job"


def append_history(entry: Dict[str, Any]) -> None:
    ensure_dirs()
    with open(HISTORY_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
