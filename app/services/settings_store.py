"""Runtime settings stored in the DB, with env-derived defaults."""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import AppSetting

BOT_SWITCH_VALUES = ("ON", "OFF", "PAUSED")

DEFAULT_HANDOFF_KEYWORDS: dict[str, list[str]] = {
    "refund": ["refund", "money back", "chargeback", "reimburse", "reimbursement"],
    "complaint": ["complaint", "complain", "complaining", "terrible service", "unacceptable", "disgusting"],
    "allergy": ["allergy", "allergies", "allergic", "allergen", "anaphylaxis", "peanut", "nut free", "gluten free"],
    "food_poisoning": ["food poisoning", "got sick", "fell sick", "vomit", "vomiting", "diarrhea", "diarrhoea", "stomach upset"],
    "legal": ["lawyer", "sue", "suing", "legal action", "court", "solicitor", "consumer protection", "fccpc"],
}

DEFAULT_HUMAN_PHRASES = [
    "human",
    "real person",
    "speak to someone",
    "talk to someone",
    "speak to a person",
    "talk to a person",
    "customer care",
    "customer service",
    "manager",
    "representative",
    "live agent",
    "an agent",
]

# Categories that get a red flag in the Dashboard App.
RED_FLAG_CATEGORIES = {"refund", "allergy", "complaint", "food_poisoning", "legal"}


def defaults() -> dict[str, Any]:
    from .llm import resolve_model

    s = get_settings()
    return {
        "bot_switch": "ON",
        "llm_provider": s.llm_provider,
        "llm_model": resolve_model(s.llm_provider, s.llm_model),
        "daily_spend_cap_usd": float(s.daily_llm_spend_cap_usd),
        "timezone": s.timezone,
        "greeting": f"Hi! I'm the virtual assistant for {s.restaurant_name}.",
        "handoff_message": "Thanks for your patience. I'm passing you to a member of our team, who will reply here shortly.",
        "handoff_keywords": copy.deepcopy(DEFAULT_HANDOFF_KEYWORDS),
        "human_request_phrases": list(DEFAULT_HUMAN_PHRASES),
        "max_bot_turns": 6,
        "confidence_threshold": 0.6,
        "currency_symbol": "₦",
        "quick_replies": {
            "hours": "Our opening hours are listed below. (Edit this quick reply in Settings.)",
            "delivery": "We deliver to selected areas. Send your area and we'll confirm the fee.",
            "payment": "We accept bank transfer, card and cash on pickup. (Edit this quick reply in Settings.)",
        },
        "llm_price_overrides": {},
    }


def get_setting(db: Session, key: str) -> Any:
    row = db.get(AppSetting, key)
    if row is not None:
        return row.value
    return defaults().get(key)


def all_settings(db: Session) -> dict[str, Any]:
    values = defaults()
    for row in db.query(AppSetting).all():
        values[row.key] = row.value
    return values


def set_setting(db: Session, key: str, value: Any) -> tuple[Any, Any]:
    """Set a value; returns (before, after). Caller commits and audits."""
    before = get_setting(db, key)
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    return before, value
