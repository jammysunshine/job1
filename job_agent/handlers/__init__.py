from typing import Optional

from .base import VendorHandler
from .careers_page import CareersPageHandler
from .people_ksa import PeopleKsaHandler
from .oracle_recruiting import OracleRecruitingHandler

HANDLER_REGISTRY: dict = {
    "careers_page": CareersPageHandler,
    "people_ksa": PeopleKsaHandler,
    "oracle_recruiting_cloud": OracleRecruitingHandler,
    "oracle_taleo": OracleRecruitingHandler,
}


def get_handler(ats_type: str) -> Optional[type]:
    return HANDLER_REGISTRY.get(ats_type)
