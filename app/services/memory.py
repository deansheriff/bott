"""Per-contact memory and conversation summaries."""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import ContactMemory, ConversationState, MessageLog, Order
from ..utils import normalize_phone, truncate, utcnow
from .copilot import summarize_conversation_text
from .spend import SpendCapReached

log = logging.getLogger(__name__)
MAX_ORDERS = 5
MAX_SUMMARIES = 5


def upsert_contact(db: Session, contact_id: int | None, name: str = "", phone: str = "") -> ContactMemory | None:
    if not contact_id:
        return None
    mem = db.get(ContactMemory, contact_id)
    if mem is None:
        mem = ContactMemory(contact_id=contact_id, name=truncate(name, 255), phone=(phone or "")[:32], last_orders=[], summaries=[])
        db.add(mem)
    else:
        if name and not mem.name:
            mem.name = truncate(name, 255)
        if phone and not mem.phone:
            mem.phone = phone[:32]
    db.flush()
    refresh_orders(db, mem)
    return mem


def refresh_orders(db: Session, mem: ContactMemory) -> None:
    """If the POS writes to the orders table, keep the last 5 orders in memory in sync."""
    q = db.query(Order)
    phone = normalize_phone(mem.phone)
    if phone:
        q = q.filter((Order.contact_id == mem.contact_id) | (Order.phone.like(f"%{phone}")))
    else:
        q = q.filter(Order.contact_id == mem.contact_id)
    orders = q.order_by(Order.created_at.desc()).limit(MAX_ORDERS).all()
    if orders:
        mem.last_orders = [{"ref": o.reference, "summary": truncate(o.items_summary, 120), "date": o.created_at.date().isoformat()} for o in orders]


def profile_for_prompt(mem: ContactMemory | None) -> dict:
    if mem is None:
        return {}
    return {
        "name": mem.name,
        "preferred_language": mem.preferred_language,
        "preferences": truncate(mem.preferences, 400),
        "notes": truncate(mem.notes, 400),
        "last_orders": (mem.last_orders or [])[:MAX_ORDERS],
        "previous_conversations": (mem.summaries or [])[-2:],
    }


def summarize_closed_conversation(db: Session, conversation_id: int) -> None:
    state = db.get(ConversationState, conversation_id)
    if state is None or state.summarized_at is not None or not state.contact_id:
        return
    rows = (
        db.query(MessageLog)
        .filter(MessageLog.conversation_id == conversation_id, MessageLog.private.is_(False))
        .order_by(MessageLog.created_at.desc())
        .limit(30)
        .all()
    )
    if not rows:
        return
    transcript = "\n".join(f"{'Customer' if r.direction == 'incoming' else 'Restaurant'}: {truncate(r.content, 300)}" for r in reversed(rows))
    try:
        summary = summarize_conversation_text(db, transcript, conversation_id)
    except SpendCapReached:
        summary = ""
    except Exception:
        log.exception("memory.summary_failed", extra={"conversation_id": conversation_id})
        summary = ""
    mem = db.get(ContactMemory, state.contact_id)
    if mem and summary:
        mem.summaries = ([*(mem.summaries or []), {"conversation_id": conversation_id, "date": utcnow().date().isoformat(), "summary": summary}])[-MAX_SUMMARIES:]
    state.summarized_at = utcnow()
    db.commit()
    log.info("memory.summarized", extra={"conversation_id": conversation_id, "ok": bool(summary)})
