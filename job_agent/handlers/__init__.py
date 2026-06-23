from typing import Optional

from .base import VendorHandler
from .careers_page import CareersPageHandler

HANDLER_REGISTRY: dict = {
    "careers_page": CareersPageHandler,
}


def get_handler(ats_type: str) -> Optional[type]:
    return HANDLER_REGISTRY.get(ats_type)
