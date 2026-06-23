from typing import Optional

from .base import VendorHandler
from .careers_page import CareersPageHandler
from .people_ksa import PeopleKsaHandler

HANDLER_REGISTRY: dict = {
    "careers_page": CareersPageHandler,
    "people_ksa": PeopleKsaHandler,
}


def get_handler(ats_type: str) -> Optional[type]:
    return HANDLER_REGISTRY.get(ats_type)
