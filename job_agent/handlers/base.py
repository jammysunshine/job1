from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..models import FieldEvidence


@dataclass
class FillResult:
    status: str
    filled_fields: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    screenshot_path: Optional[str] = None
    form_url: Optional[str] = None


class VendorHandler(ABC):
    @abstractmethod
    async def discover_fields(self, url: str) -> tuple:
        ...

    @abstractmethod
    async def fill_and_stop(
        self,
        url: str,
        field_mapping: Dict[str, Any],
        cv_path: Optional[str] = None,
    ) -> FillResult:
        ...
