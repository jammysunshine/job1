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
    user_note: Optional[str] = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    ats_type: Optional[str] = None
    ats_confidence: Optional[float] = None
    evidence_path: Optional[str] = None
    screenshot_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FieldEvidence:
    field_idx: int
    field_id: str
    tag_name: str
    input_type: Optional[str]
    label: Optional[str]
    placeholder: Optional[str]
    aria_label: Optional[str]
    required: bool
    visible: bool
    options: List[str] = field(default_factory=list)
    nearby_text: Optional[str] = None
    page_idx: Optional[int] = None
    iframe_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PageEvidence:
    job_url: str
    final_url: str
    title: str
    visible_text_sample: str
    ats_signals: Dict[str, Any]
    fields: List[FieldEvidence]
    buttons: List[str]
    captured_at: str = field(default_factory=utc_now_iso)
    screenshot_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["fields"] = [field.to_dict() for field in self.fields]
        return data

