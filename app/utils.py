from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utcnow() -> datetime:
    """Naive UTC datetime. All DB timestamps are stored as naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def safe_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Africa/Lagos")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Africa/Lagos")


def is_valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def to_local(dt_utc_naive: datetime | None, tz_name: str) -> datetime | None:
    if dt_utc_naive is None:
        return None
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone(safe_tz(tz_name))


def normalize_phone(phone: str | None) -> str:
    """Compare phones by their last 10 digits (+234 803... == 0803...)."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else digits


def truncate(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def csv_safe(value: object) -> str:
    """Neutralise spreadsheet formula injection in CSV exports."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def format_money(amount: float | None, symbol: str) -> str:
    if amount is None:
        return ""
    amount = float(amount)
    if amount.is_integer():
        return f"{symbol}{amount:,.0f}"
    return f"{symbol}{amount:,.2f}"
