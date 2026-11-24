"""Time helpers. The canonical model uses ISO 8601 with the WIB offset (+07:00)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from common.config import SIM_DATE, WINDOW_HOURS, WINDOW_START_HOUR

WIB = timezone(timedelta(hours=7), name="WIB")
UTC = timezone.utc


def window_start(d: date = SIM_DATE, start_hour: int = WINDOW_START_HOUR) -> datetime:
    return datetime(d.year, d.month, d.day, start_hour, tzinfo=WIB)


def slot_starts(d: date = SIM_DATE, start_hour: int = WINDOW_START_HOUR,
                hours: int = WINDOW_HOURS) -> list[datetime]:
    start = window_start(d, start_hour)
    return [start + timedelta(hours=i) for i in range(hours)]


def slot_labels(d: date = SIM_DATE, start_hour: int = WINDOW_START_HOUR,
                hours: int = WINDOW_HOURS) -> list[str]:
    return [s.strftime("%H:%M") for s in slot_starts(d, start_hour, hours)]


def now_wib() -> datetime:
    return datetime.now(WIB)


def now_iso() -> str:
    return now_wib().isoformat(timespec="seconds")


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")
