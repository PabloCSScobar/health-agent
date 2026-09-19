"""Wspólne granice kalendarzowe aplikacji w skonfigurowanej strefie."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from health_agent.settings import settings


def app_timezone() -> ZoneInfo:
    return ZoneInfo(settings.summary_timezone)


def local_now() -> dt.datetime:
    return dt.datetime.now(app_timezone())


def local_today() -> dt.date:
    return local_now().date()


def local_date(value: dt.datetime) -> dt.date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(app_timezone()).date()


def utc_day_bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """Początek i koniec lokalnej doby przeliczone na UTC (obsługuje DST)."""
    start_local = dt.datetime.combine(day, dt.time.min, tzinfo=app_timezone())
    end_local = start_local + dt.timedelta(days=1)
    return start_local.astimezone(dt.timezone.utc), end_local.astimezone(dt.timezone.utc)
