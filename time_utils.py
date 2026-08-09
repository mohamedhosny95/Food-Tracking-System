"""Single source of truth for the user's calendar and clock."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import config


APP_TIMEZONE = ZoneInfo(config.TIMEZONE_NAME)


def local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def local_today() -> date:
    return local_now().date()
