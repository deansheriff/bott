"""Admin: customer data the bot stores (contact memory, transcripts, bot decisions).

Deleting here removes the bot's copy only. Chatwoot keeps its own conversation history;
delete that in Chatwoot if a customer asks for their data to be erased.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import Auth, require
from ..models import BotTurn, ContactMemory, ConversationState, MessageLog, ToolCallLog
from ..services import audit
from ..templating import render

router = APIRouter(prefix="/admin/contacts")
PAGE = 50
# Fixed messages for ?done=... (never echo free text from the URL).
DONE = {"saved": "Saved.", "memory": "Contact memory deleted.", "all": "All stored data for that customer was deleted."}
MEMORY_FIELDS = ["name", "phone", "preferred_language", "preferences", "notes", "last_orders"]


def _conversation_ids(db: Session, contact_id: int) -> list[int]:
    return [c for (c,) in db.query(ConversationState.conversation_id).filter(ConversationState.contact_id == contact_id).all()]


@router.get("", response_class=HTMLResponse)
def contacts_list(request: Request, q: str = "", page: int = 1, done: str = "", auth: Auth = Depends(require("contacts.view")), db: Session = Depends(get_db)):
    query = db.query(ContactMemory)
    q = q.strip()[:100]
    if q:
        like = f"%{q}%"
        conds = [ContactMemory.name.ilike(like), ContactMemory.phone.ilike(like)]
        digits = re.sub(r"\D", "", q)
        if q.isdigit():
            conds.append(ContactMemory.contact_id == int(q))
        # Match local and international forms: "0803 000 0000" finds "+2348030000000".
        core = digits[3:] if digits.startswith("234") else digits.lstrip("0")
        if len(core) >= 3:
            conds.append(ContactMemory.phone.ilike(f"%{core}%"))
        query = query.filter(or_(*conds))
    total = query.count()
    page = max(1, page)
    rows = query.order_by(ContactMemory.updated_at.desc()).offset((page - 1) * PAGE).limit(PAGE).all()
    conv_counts = dict(db.query(ConversationState.contact_id, func.count(ConversationState.conversation_id)).filter(ConversationState.contact_id.in_([r.contact_id for r in rows] or [-1])).group_by(ConversationState.contact_id).all())
    return render(request, "admin/contacts_list.html", {"rows": rows, "q": q, "page": page, "pages": max(1, (total + PAGE - 1) // PAGE), "total": total, "conv_counts": conv_counts, "flash": DONE.get(done, "")}, auth=auth, db=db)


@router.get("/{contact_id}", response_class=HTMLResponse)
def contact_detail(request: Request, contact_id: int, done: str = "", auth: Auth = Depends(require("contacts.view")), db: Session = Depends(get_db)):
    mem = db.get(ContactMemory, contact_id)
    conv_ids = _conversation_ids(db, contact_id)
    if mem is None and not conv_ids:
        return render(request, "admin/message.html", {"title": "Not found", "message": "No stored data for this customer."}, auth=auth, db=db, status_code=404)
    conversations = db.query(ConversationState).filter(ConversationState.contact_id == contact_id).order_by(ConversationState.updated_at.desc()).all()
    messages = db.query(func.count(MessageLog.id)).filter(MessageLog.conversation_id.in_(conv_ids or [-1])).scalar()
    return render(request, "admin/contact_detail.html", {"mem": mem, "contact_id": contact_id, "conversations": conversations, "message_count": messages, "flash": DONE.get(done, "")}, auth=auth, db=db)


@router.post("/{contact_id}", response_class=HTMLResponse)
def contact_save(
    request: Request,
    contact_id: int,
    name: str = Form(""),
    phone: str = Form(""),
    preferred_language: str = Form(""),
    preferences: str = Form(""),
    notes: str = Form(""),
    last_orders: str = Form(""),
    auth: Auth = Depends(require("contacts.edit")),
    db: Session = Depends(get_db),
):
    mem = db.get(ContactMemory, contact_id)
    if mem is None:
        return render(request, "admin/message.html", {"title": "Not found", "message": "No stored memory for this customer."}, auth=auth, db=db, status_code=404)
    before = audit.model_snapshot(mem, MEMORY_FIELDS)
    mem.name, mem.phone = name.strip()[:255], phone.strip()[:32]
    mem.preferred_language = preferred_language.strip()[:16]
    mem.preferences, mem.notes = preferences.strip()[:2000], notes.strip()[:4000]
    mem.last_orders = [{"ref": "", "summary": line.strip()[:200], "date": ""} for line in last_orders.splitlines() if line.strip()][:5]
    audit.record(db, auth.user, "contact.update", "contact_memory", contact_id, before, audit.model_snapshot(mem, MEMORY_FIELDS), auth.ip)
    db.commit()
    return RedirectResponse(f"/admin/contacts/{contact_id}?done=saved", status_code=303)


def erase_contact(db: Session, contact_id: int, everything: bool) -> dict[str, int]:
    """Delete the bot's stored data for a customer. Returns counts per table."""
    counts = {"memory": db.query(ContactMemory).filter(ContactMemory.contact_id == contact_id).delete(synchronize_session=False)}
    if everything:
        conv_ids = _conversation_ids(db, contact_id) or [-1]
        turn_ids = [t for (t,) in db.query(BotTurn.id).filter(BotTurn.conversation_id.in_(conv_ids)).all()] or [-1]
        counts["tool_calls"] = db.query(ToolCallLog).filter(or_(ToolCallLog.conversation_id.in_(conv_ids), ToolCallLog.bot_turn_id.in_(turn_ids))).delete(synchronize_session=False)
        counts["bot_decisions"] = db.query(BotTurn).filter(BotTurn.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
        counts["messages"] = db.query(MessageLog).filter(MessageLog.conversation_id.in_(conv_ids)).delete(synchronize_session=False)
        counts["conversations"] = db.query(ConversationState).filter(ConversationState.contact_id == contact_id).delete(synchronize_session=False)
    return counts


@router.post("/{contact_id}/delete")
def contact_delete(request: Request, contact_id: int, scope: str = Form("memory"), auth: Auth = Depends(require("contacts.delete")), db: Session = Depends(get_db)):
    everything = scope == "all"
    counts = erase_contact(db, contact_id, everything)
    # Counts only: the audit log must not keep a copy of the data that was erased.
    audit.record(db, auth.user, "contact.erase_all" if everything else "contact.delete_memory", "contact", contact_id, None, counts, auth.ip)
    db.commit()
    return RedirectResponse(f"/admin/contacts?done={'all' if everything else 'memory'}", status_code=303)
