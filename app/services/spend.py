"""LLM spend tracking and the daily cap."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import LLMUsage
from ..utils import safe_tz
from . import settings_store
from .llm import LLMProvider, LLMResponse, ToolSpec, get_provider, resolve_model
from .llm.pricing import cost_usd

log = logging.getLogger(__name__)


class SpendCapReached(Exception):
    pass


def local_day_bounds_utc(tz_name: str, day: datetime | None = None) -> tuple[datetime, datetime]:
    tz = safe_tz(tz_name)
    local = (day or datetime.now(timezone.utc)).astimezone(tz)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).replace(tzinfo=None), end.astimezone(timezone.utc).replace(tzinfo=None))


def spend_today(db: Session) -> float:
    start, end = local_day_bounds_utc(settings_store.get_setting(db, "timezone"))
    total = db.scalar(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(LLMUsage.created_at >= start, LLMUsage.created_at < end))
    return float(total or 0.0)


def cap_reached(db: Session) -> bool:
    cap = float(settings_store.get_setting(db, "daily_spend_cap_usd") or 0)
    return cap <= 0 or spend_today(db) >= cap


def tracked_complete(
    db: Session,
    purpose: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[ToolSpec] | None = None,
    json_mode: bool = False,
    conversation_id: int | None = None,
    max_tokens: int = 700,
    provider: LLMProvider | None = None,
) -> tuple[LLMResponse, float]:
    """Call the LLM if the cap allows, record usage and cost. Returns (response, cost_usd)."""
    if cap_reached(db):
        raise SpendCapReached()
    provider_name = settings_store.get_setting(db, "llm_provider")
    provider = provider or get_provider(provider_name)
    model = resolve_model(provider_name, settings_store.get_setting(db, "llm_model"))
    resp = provider.complete(messages, model=model, tools=tools, json_mode=json_mode, max_tokens=max_tokens)
    cost = cost_usd(resp.model or model, resp.input_tokens, resp.output_tokens, settings_store.get_setting(db, "llm_price_overrides"))
    db.add(
        LLMUsage(
            model=resp.model or model,
            purpose=purpose,
            input_tokens=resp.input_tokens,
            output_tokens=resp.output_tokens,
            cost_usd=cost,
            conversation_id=conversation_id,
        )
    )
    db.commit()
    log.info("llm.call", extra={"purpose": purpose, "model": resp.model or model, "in_tokens": resp.input_tokens, "out_tokens": resp.output_tokens, "conversation_id": conversation_id})
    return resp, cost
