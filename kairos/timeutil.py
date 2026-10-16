"""Time helpers. All graph timestamps are epoch milliseconds (UTC)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone, tzinfo

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS

_FALLBACK_OFFSETS = {"Asia/Kolkata": timedelta(hours=5, minutes=30), "UTC": timedelta(0)}


def now_ms() -> int:
    return int(time.time() * 1000)


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # Windows without the tzdata package
        return timezone(_FALLBACK_OFFSETS.get(name, timedelta(0)), name)


def to_local(ts: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts / 1000, get_tz(tz))


def weekday_hour(ts: int, tz: str) -> tuple[int, int]:
    """Monday=0 weekday and hour of day in the user's timezone."""
    dt = to_local(ts, tz)
    return dt.weekday(), dt.hour


def start_of_day(tz: str, days_ago: int = 0, now: int | None = None) -> int:
    dt = to_local(now if now is not None else now_ms(), tz)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(start.timestamp() * 1000)


PERIODS = ("today", "yesterday", "yesterday_morning", "yesterday_evening", "last_week", "last_month")


def period_range(period: str | None, tz: str, now: int, default_days: int = 30) -> tuple[int, int]:
    """Named time window -> (since, until) epoch ms. Used by both the rule and LLM planners."""
    today = start_of_day(tz, 0, now)
    if period == "today":
        return today, now + 1
    if period in ("yesterday", "yesterday_morning", "yesterday_evening"):
        start, end = today - DAY_MS, today
        if period == "yesterday_evening":
            start += 17 * HOUR_MS
        elif period == "yesterday_morning":
            end = start + 12 * HOUR_MS
        return start, end
    if period == "last_week":
        return now - 7 * DAY_MS, now + 1
    if period == "last_month":
        return now - 30 * DAY_MS, now + 1
    return now - default_days * DAY_MS, now + 1


def fmt_time(ts: int | None, tz: str) -> str:
    if not ts:
        return "unknown time"
    dt = to_local(ts, tz)
    today = to_local(now_ms(), tz).date()
    hm = dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() == today:
        return f"today at {hm}"
    if dt.date() == today - timedelta(days=1):
        return f"yesterday at {hm}"
    return dt.strftime("%a %d %b, ") + hm
