"""Opening status from the weekly hours table plus date overrides."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.orm import Session

from ..models import DateOverride, WeeklyHours
from ..utils import safe_tz

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def valid_hhmm(value: str) -> bool:
    return bool(TIME_RE.match(value or ""))


def _parse(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def _span(day: date, open_s: str, close_s: str, tz) -> tuple[datetime, datetime] | None:
    if not (valid_hhmm(open_s) and valid_hhmm(close_s)):
        return None
    start = datetime.combine(day, _parse(open_s), tzinfo=tz)
    end = datetime.combine(day, _parse(close_s), tzinfo=tz)
    if end <= start:  # closes after midnight
        end += timedelta(days=1)
    return start, end


def spans_for(db: Session, day: date, tz) -> tuple[list[tuple[datetime, datetime]], str]:
    """Return (opening spans, note) for a local date."""
    override = db.query(DateOverride).filter(DateOverride.day == day).one_or_none()
    if override is not None:
        if override.closed:
            return [], override.note or "Closed (special date)"
        span = _span(day, override.open_time, override.close_time, tz)
        return ([span] if span else []), override.note or "Special hours"
    row = db.get(WeeklyHours, day.weekday())
    if row is None or row.closed:
        return [], ""
    span = _span(day, row.open_time, row.close_time, tz)
    return ([span] if span else []), ""


def hours_configured(db: Session) -> bool:
    return db.query(WeeklyHours).count() > 0


def opening_status(db: Session, tz_name: str, now: datetime | None = None) -> dict:
    tz = safe_tz(tz_name)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    if not hours_configured(db):
        return {"configured": False, "open_now": None, "message": "Opening hours have not been configured.", "local_time": now_local.strftime("%A %H:%M")}

    today = now_local.date()
    open_now = False
    closes_at = None
    for d in (today - timedelta(days=1), today):
        spans, _ = spans_for(db, d, tz)
        for start, end in spans:
            if start <= now_local < end:
                open_now, closes_at = True, end

    today_spans, note = spans_for(db, today, tz)
    today_text = ", ".join(f"{s:%H:%M}–{e:%H:%M}" for s, e in today_spans) or "closed"

    next_open = None
    if not open_now:
        for offset in range(0, 8):
            spans, _ = spans_for(db, today + timedelta(days=offset), tz)
            future = [s for s, _ in spans if s > now_local]
            if future:
                next_open = min(future)
                break

    return {
        "configured": True,
        "open_now": open_now,
        "local_time": now_local.strftime("%A %H:%M"),
        "timezone": tz_name,
        "today_hours": today_text,
        "closes_at": closes_at.strftime("%H:%M") if closes_at else None,
        "next_open": next_open.strftime("%A %H:%M") if next_open else None,
        "note": note,
    }
