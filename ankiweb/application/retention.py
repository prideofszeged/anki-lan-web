"""Backup retention policy (SPEC section 15: 7 daily, 4 weekly, 6 monthly). Pure functions."""
from __future__ import annotations
import re
from datetime import date, datetime, timedelta, timezone

_NAME_RE = re.compile(r"^anki-lan-web-(\d{8}T\d{6}Z)\.tar\.gz$")
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def parse_stamp(filename: str) -> datetime | None:
    """UTC timestamp encoded in a backup archive name, or None for any other file."""
    m = _NAME_RE.match(filename)
    return datetime.strptime(m.group(1), STAMP_FORMAT).replace(tzinfo=timezone.utc) if m else None


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def select_keep(stamps, now: datetime, *, daily: int = 7, weekly: int = 4,
                monthly: int = 6) -> set[datetime]:
    """Newest backup per day (last ``daily`` days), per ISO week (last ``weekly`` weeks) and per
    month (last ``monthly`` months), plus always the single newest. Everything else may go."""
    ordered = sorted(set(stamps), reverse=True)
    if not ordered:
        return set()
    today = now.date()
    day_floor = today - timedelta(days=daily - 1)
    week_floor = _monday(today) - timedelta(weeks=weekly - 1)
    month_floor = _month_index(today) - (monthly - 1)
    keep = {ordered[0]}
    seen_days: set[date] = set()
    seen_weeks: set[date] = set()
    seen_months: set[int] = set()
    for stamp in ordered:                       # newest first: first hit in a bucket is its newest
        d = stamp.date()
        if d >= day_floor and d not in seen_days:
            seen_days.add(d)
            keep.add(stamp)
        week = _monday(d)
        if week >= week_floor and week not in seen_weeks:
            seen_weeks.add(week)
            keep.add(stamp)
        month = _month_index(d)
        if month >= month_floor and month not in seen_months:
            seen_months.add(month)
            keep.add(stamp)
    return keep
