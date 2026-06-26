from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    job_url: str
    status: str = "received"
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    form_url: Optional[str] = None
    ats_type: Optional[str] = None
    cv_variant: Optional[str] = None
    filled_count: int = 0
    error_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlanStep:
    action: str  # click | fill | select | upload | checkbox
    idx: Optional[int] = None  # element index from parsed snapshot
    value: Optional[str] = None  # for fill/select/upload
    reason: Optional[str] = None  # LLM's reasoning

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> PlanStep:
        idx = d.get("idx")
        if idx is not None:
            idx = int(idx)
        return PlanStep(
            action=d["action"],
            idx=idx,
            value=d.get("value"),
            reason=d.get("reason"),
        )


@dataclass
class Plan:
    steps: List[PlanStep]
    cv_variant: Optional[str] = None
    notes: Optional[str] = None
    ask_user: Optional[str] = None  # question to ask user, if unsure
    stop_for_review: bool = False  # hard stop before submit

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> Plan:
        return Plan(
            steps=[PlanStep.from_dict(s) for s in d.get("steps", [])],
            cv_variant=d.get("cv_variant"),
            notes=d.get("notes"),
            ask_user=d.get("ask_user"),
            stop_for_review=d.get("stop_for_review", False),
        )


@dataclass
class PageState:
    url: str
    title: str
    aria_snapshot: str
    screenshot_path: str
