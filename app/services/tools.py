"""Restaurant tools the model can call. Prices, fees, hours and order status come ONLY from here."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from ..models import DeliveryZone, MenuItem, Order, ToolCallLog
from ..utils import format_money, normalize_phone, safe_tz
from .audit import scrub
from .hours import opening_status
from .llm import ToolSpec
from .retriever import tokenize

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    db: Session
    tz: str
    currency: str
    contact_phone: str | None = None
    contact_id: int | None = None
    conversation_id: int | None = None
    bot_turn_id: int | None = None


TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        "menu_lookup",
        "Look up menu items, prices and today's availability. Always use this before mentioning any item or price.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Dish name or keywords, e.g. 'jollof rice'. Empty to list."},
                "category": {"type": "string", "description": "Optional category, e.g. 'drinks'."},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "order_status",
        "Get the status of an order by order reference, or the customer's recent orders by their phone number.",
        {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "Order reference, e.g. 'A1234'."},
                "phone": {"type": "string", "description": "Customer phone number."},
            },
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "delivery_check",
        "Check whether we deliver to an address or area, and the delivery fee and minimum order.",
        {
            "type": "object",
            "properties": {"area": {"type": "string", "description": "Address, street, or area name."}},
            "required": ["area"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        "opening_status",
        "Whether the restaurant is open right now, today's hours, and when it next opens.",
        {"type": "object", "properties": {}, "additionalProperties": False},
    ),
]


def _local_today(ctx: ToolContext):
    return datetime.now(timezone.utc).astimezone(safe_tz(ctx.tz)).date()


def item_status(item: MenuItem, today) -> str:
    if not item.available:
        return "unavailable"
    if item.eighty_sixed_on == today:
        return "sold out today"
    return "available"


def menu_lookup(ctx: ToolContext, query: str = "", category: str = "") -> dict:
    today = _local_today(ctx)
    items = ctx.db.query(MenuItem).order_by(MenuItem.category, MenuItem.name).all()
    if category:
        cat = category.strip().lower()
        items = [i for i in items if cat in (i.category or "").lower()]
    q = set(tokenize(query or ""))
    if q:
        scored = []
        for i in items:
            name_toks = set(tokenize(i.name))
            other = set(tokenize(f"{i.category} {i.description}"))
            score = 3 * len(q & name_toks) + len(q & other)
            if score:
                scored.append((score, i))
        scored.sort(key=lambda x: -x[0])
        items = [i for _, i in scored]
    items = items[:12]
    return {
        "query": query,
        "count": len(items),
        "items": [
            {
                "name": i.name,
                "category": i.category,
                "price": float(i.price),
                "price_display": format_money(i.price, ctx.currency),
                "status": item_status(i, today),
                "description": (i.description or "")[:200],
            }
            for i in items
        ],
        "note": "Only items listed here exist. If count is 0 the item is not on the menu.",
    }


def order_status(ctx: ToolContext, order_id: str = "", phone: str = "") -> dict:
    contact_phone = normalize_phone(ctx.contact_phone)
    if order_id:
        ref = re.sub(r"[^A-Za-z0-9-]", "", order_id)[:64]
        order = ctx.db.query(Order).filter(Order.reference.ilike(ref)).one_or_none() if ref else None
        if order is None:
            return {"found": False, "message": "No order with that reference."}
        if order.phone and contact_phone and normalize_phone(order.phone) != contact_phone:
            return {"found": True, "verified": False, "message": "This order is linked to a different phone number. A team member must verify the customer."}
        result = {"found": True, "reference": order.reference, "status": order.status, "updated": order.updated_at.isoformat(timespec="minutes")}
        if contact_phone:  # only show contents when the phone on the conversation matches
            result["items"] = order.items_summary
            result["total_display"] = format_money(order.total, ctx.currency)
        return result
    if phone:
        if not contact_phone or normalize_phone(phone) != contact_phone:
            return {"found": False, "message": "For privacy, orders can only be looked up for the phone number on this conversation."}
        orders = ctx.db.query(Order).filter(Order.phone.like(f"%{contact_phone}")).order_by(Order.created_at.desc()).limit(3).all()
        return {
            "found": bool(orders),
            "orders": [{"reference": o.reference, "status": o.status, "created": o.created_at.isoformat(timespec="minutes"), "items": o.items_summary} for o in orders],
        }
    return {"found": False, "message": "Provide an order reference or phone number."}


def _norm_area(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


def delivery_check(ctx: ToolContext, area: str = "") -> dict:
    target = _norm_area(area)
    if not target.strip():
        return {"deliverable": False, "message": "Please provide an area or address."}
    best: tuple[int, DeliveryZone] | None = None
    for zone in ctx.db.query(DeliveryZone).filter(DeliveryZone.active.is_(True)).all():
        for name in [zone.name, *zone.alias_list()]:
            n = _norm_area(name)
            if n.strip() and n in target and (best is None or len(n) > best[0]):
                best = (len(n), zone)
    if best is None:
        return {"deliverable": False, "area": area, "message": "This area is not in our delivery zones."}
    z = best[1]
    return {
        "deliverable": True,
        "zone": z.name,
        "fee": float(z.fee),
        "fee_display": format_money(z.fee, ctx.currency),
        "minimum_order": float(z.min_order or 0),
        "minimum_order_display": format_money(z.min_order or 0, ctx.currency),
    }


def opening_status_tool(ctx: ToolContext) -> dict:
    return opening_status(ctx.db, ctx.tz)


_REGISTRY: dict[str, Callable[..., dict]] = {
    "menu_lookup": menu_lookup,
    "order_status": order_status,
    "delivery_check": delivery_check,
    "opening_status": opening_status_tool,
}
_ALLOWED_ARGS = {"menu_lookup": {"query", "category"}, "order_status": {"order_id", "phone"}, "delivery_check": {"area"}, "opening_status": set()}


def run_tool(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict:
    started = time.perf_counter()
    fn = _REGISTRY.get(name)
    clean_args = {k: str(v)[:200] for k, v in (args or {}).items() if k in _ALLOWED_ARGS.get(name, set())}
    ok = True
    if fn is None:
        out, ok = {"error": f"unknown tool {name}"}, False
    else:
        try:
            out = fn(ctx, **clean_args)
        except Exception:
            log.exception("tool.error", extra={"tool": name})
            out, ok = {"error": "tool failed"}, False
    duration = int((time.perf_counter() - started) * 1000)
    ctx.db.add(ToolCallLog(bot_turn_id=ctx.bot_turn_id, conversation_id=ctx.conversation_id, tool=name, inputs=scrub(clean_args), outputs=scrub(out), ok=ok, duration_ms=duration))
    ctx.db.flush()
    log.info("tool.call", extra={"tool": name, "ok": ok, "duration_ms": duration, "conversation_id": ctx.conversation_id})
    return out
