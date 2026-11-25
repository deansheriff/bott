"""Dashboard App served inside Chatwoot's conversation view (iframe).

Auth: no cookies (third-party cookies are blocked in iframes). The panel pairs with a
logged-in admin-site session through a one-time link code, then uses a bearer token
plus a CSRF header. Conversation access is verified with the agent's OWN Chatwoot token,
never with data posted by the parent window.
"""
from __future__ import annotations

import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import Auth, require_panel
from ..models import BotTurn, ContactMemory, ConversationState, MessageLog, PanelLink
from ..security.crypto import decrypt_secret
from ..security.middleware import client_ip
from ..security.ratelimit import limiter
from ..security.sessions import create_session
from ..services import audit, settings_store
from ..services.chatwoot import ChatwootError, agent_client
from ..services.copilot import draft_reply
from ..services.llm import LLMError
from ..services.memory import profile_for_prompt
from ..services.settings_store import RED_FLAG_CATEGORIES
from ..services.spend import SpendCapReached
from ..templating import render
from ..utils import random_token, sha256_hex, utcnow

router = APIRouter(prefix="/app-panel")
LINK_TTL = timedelta(minutes=10)
ACCESS_CACHE_SECONDS = 60
_access_cache: dict[tuple[int, str, int], tuple[float, dict]] = {}


class NoAccess(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


@router.get("/", response_class=HTMLResponse)
def panel_shell(request: Request):
    return render(request, "panel/shell.html", {"chatwoot_origin": get_settings().chatwoot_browser_origin})


# ------------------------------------------------------------------ pairing
@router.post("/api/link/start")
def link_start(request: Request, db: Session = Depends(get_db)):
    if not limiter.hit(f"panel-link:{client_ip(request)}", 10, 600):
        return JSONResponse({"error": "Too many attempts, try again later."}, status_code=429)
    nonce = random_token(32)
    code = "-".join(secrets.token_hex(2).upper() for _ in range(2))
    db.add(PanelLink(nonce_hash=sha256_hex(nonce), code=code, expires_at=utcnow() + LINK_TTL))
    db.commit()
    return {"nonce": nonce, "code": code, "login_url": f"/admin/login?next=/admin/panel-link%3Fcode%3D{code}"}


@router.post("/api/link/claim")
async def link_claim(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    nonce = str(body.get("nonce") or "")
    link = db.query(PanelLink).filter(PanelLink.nonce_hash == sha256_hex(nonce)).one_or_none() if nonce else None
    if link is None or link.expires_at < utcnow() or link.claimed_at is not None:
        return JSONResponse({"status": "expired"}, status_code=410)
    if link.confirmed_at is None or link.user_id is None:
        return JSONResponse({"status": "pending"}, status_code=202)
    link.claimed_at = utcnow()
    from ..models import User

    user = db.get(User, link.user_id)
    if user is None or not user.is_active:
        db.commit()
        return JSONResponse({"status": "expired"}, status_code=410)
    raw, sess = create_session(db, user, "panel", client_ip(request), request.headers.get("user-agent"))
    audit.record(db, user, "panel.linked", "user", user.id, None, {"session": sess.id[:8]}, client_ip(request))
    db.commit()
    return {"status": "ok", "token": raw, "csrf": sess.csrf_token, "email": user.email}


@router.post("/api/logout")
def panel_logout(auth: Auth = Depends(require_panel()), db: Session = Depends(get_db)):
    db.delete(auth.session)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ access
def _agent_token(auth: Auth) -> str:
    token = decrypt_secret(auth.user.chatwoot_token_enc)
    if not token:
        raise NoAccess("no_token")
    return token


def verify_access(auth: Auth, conversation_id: int) -> dict:
    """Ask Chatwoot, with the agent's own token, whether they can see this conversation."""
    token = _agent_token(auth)
    # Keyed on the token too, so replacing/removing a token invalidates cached access.
    key = (auth.user.id, sha256_hex(token)[:16], conversation_id)
    hit = _access_cache.get(key)
    if hit and time.monotonic() - hit[0] < ACCESS_CACHE_SECONDS:
        return hit[1]
    if len(_access_cache) > 5000:
        _access_cache.clear()
    try:
        conv = agent_client(token).get_conversation(conversation_id)
    except ChatwootError as exc:
        raise NoAccess("token_invalid" if exc.status == 401 else "no_access") from exc
    sender = ((conv.get("meta") or {}).get("sender") or {}) if isinstance(conv, dict) else {}
    info = {
        "status": conv.get("status"),
        "contact_id": sender.get("id"),
        "contact_name": sender.get("name") or "",
        "contact_phone": sender.get("phone_number") or "",
    }
    _access_cache[key] = (time.monotonic(), info)
    return info


def _no_access_response(request: Request, reason: str, auth: Auth):
    # 200 so htmx swaps it in (htmx ignores 4xx bodies by default); the reason is in the body.
    return render(request, "panel/no_access.html", {"reason": reason}, auth=auth, headers={"X-Panel-Access": reason})


def _conversation_context(db: Session, conversation_id: int, info: dict) -> dict:
    state = db.get(ConversationState, conversation_id)
    latest = db.query(BotTurn).filter(BotTurn.conversation_id == conversation_id, BotTurn.dry_run.is_(False)).order_by(BotTurn.id.desc()).first()
    transcript = db.query(MessageLog).filter(MessageLog.conversation_id == conversation_id).order_by(MessageLog.created_at.desc(), MessageLog.id.desc()).limit(12).all()
    mem = db.get(ContactMemory, info["contact_id"]) if info.get("contact_id") else None
    status = info.get("status") or (state.last_status if state else "")
    if state and state.bot_state == "paused":
        bot_status = "paused"
    elif state and state.bot_state == "handed_off" or status in ("open", "snoozed"):
        bot_status = "handed_off"
    elif status == "resolved":
        bot_status = "resolved"
    else:
        bot_status = "active"
    return {
        "conversation_id": conversation_id,
        "state": state,
        "bot_status": bot_status,
        "chatwoot_status": status,
        "red_flag": bool(state and state.handoff_category in RED_FLAG_CATEGORIES),
        "latest": latest,
        "transcript": list(reversed(transcript)),
        "memory": mem,
        "contact_name": info.get("contact_name"),
        "quick_replies": settings_store.get_setting(db, "quick_replies") or {},
    }


def _view(request: Request, db: Session, auth: Auth, conversation_id: int, flash: str = "", draft: str = ""):
    try:
        info = verify_access(auth, conversation_id)
    except NoAccess as exc:
        return _no_access_response(request, exc.reason, auth)
    ctx = _conversation_context(db, conversation_id, info)
    ctx.update(flash=flash, draft=draft)
    return render(request, "panel/view.html", ctx, auth=auth, db=db)


@router.get("/view", response_class=HTMLResponse)
def panel_view(request: Request, conversation_id: int, auth: Auth = Depends(require_panel()), db: Session = Depends(get_db)):
    return _view(request, db, auth, conversation_id)


# ------------------------------------------------------------------ actions
def _set_status(request: Request, db: Session, auth: Auth, conversation_id: int, status: str, bot_state: str, action: str):
    try:
        verify_access(auth, conversation_id)
        agent_client(_agent_token(auth)).toggle_status(conversation_id, status)
    except NoAccess as exc:
        return _no_access_response(request, exc.reason, auth)
    except ChatwootError:
        return _view(request, db, auth, conversation_id, flash="Chatwoot rejected the status change.")
    state = db.get(ConversationState, conversation_id) or ConversationState(conversation_id=conversation_id, bot_turns=0)
    db.add(state)
    before = {"bot_state": state.bot_state, "status": state.last_status}
    state.bot_state, state.last_status = bot_state, status
    if bot_state == "active":
        state.bot_turns, state.handoff_reason, state.handoff_category = 0, "", ""
    else:
        state.handoff_at = utcnow()
    audit.record(db, auth.user, action, "conversation", conversation_id, before, {"bot_state": bot_state, "status": status}, auth.ip)
    db.commit()
    for key in [k for k in _access_cache if k[0] == auth.user.id and k[2] == conversation_id]:
        _access_cache.pop(key, None)
    return _view(request, db, auth, conversation_id, flash="Updated.")


@router.post("/api/conversations/{conversation_id}/takeover", response_class=HTMLResponse)
def takeover(request: Request, conversation_id: int, auth: Auth = Depends(require_panel("panel.act")), db: Session = Depends(get_db)):
    return _set_status(request, db, auth, conversation_id, "open", "paused", "panel.takeover")


@router.post("/api/conversations/{conversation_id}/return-to-bot", response_class=HTMLResponse)
def return_to_bot(request: Request, conversation_id: int, auth: Auth = Depends(require_panel("panel.act")), db: Session = Depends(get_db)):
    return _set_status(request, db, auth, conversation_id, "pending", "active", "panel.return_to_bot")


@router.post("/api/conversations/{conversation_id}/draft", response_class=HTMLResponse)
def generate_draft(request: Request, conversation_id: int, auth: Auth = Depends(require_panel("panel.act")), db: Session = Depends(get_db)):
    try:
        info = verify_access(auth, conversation_id)
    except NoAccess as exc:
        return _no_access_response(request, exc.reason, auth)
    rows = db.query(MessageLog).filter(MessageLog.conversation_id == conversation_id, MessageLog.private.is_(False)).order_by(MessageLog.created_at.desc(), MessageLog.id.desc()).limit(11).all()
    rows = list(reversed(rows))
    last_in = next((r for r in reversed(rows) if r.direction == "incoming"), None)
    if last_in is None:
        return render(request, "panel/_draft.html", {"draft": "", "note": "No customer message to reply to yet.", "conversation_id": conversation_id}, auth=auth)
    history = [{"role": "user" if r.direction == "incoming" else "assistant", "content": r.content} for r in rows if r.id != last_in.id and r.content]
    mem = db.get(ContactMemory, info["contact_id"]) if info.get("contact_id") else None
    note = ""
    try:
        draft, confidence = draft_reply(db, last_in.content, history[-10:], profile_for_prompt(mem), info.get("contact_phone"), info.get("contact_id"), conversation_id)
        if not draft:
            note = "The knowledge base doesn't cover this question; please write the reply yourself."
        else:
            note = f"Draft confidence {confidence:.2f}. Check it before sending."
    except SpendCapReached:
        draft, note = "", "Daily LLM spend cap reached; drafts are disabled."
    except LLMError:
        draft, note = "", "The LLM is unavailable right now."
    db.commit()
    return render(request, "panel/_draft.html", {"draft": draft, "note": note, "conversation_id": conversation_id}, auth=auth)


@router.post("/api/conversations/{conversation_id}/send", response_class=HTMLResponse)
def send_as_agent(request: Request, conversation_id: int, content: str = Form(...), private: str = Form("0"), auth: Auth = Depends(require_panel("panel.act")), db: Session = Depends(get_db)):
    content = content.strip()
    is_private = private == "1"
    if not content or len(content) > 4000:
        return _view(request, db, auth, conversation_id, flash="Message must be 1–4000 characters.", draft=content)
    try:
        verify_access(auth, conversation_id)
        # Sent with the agent's own token, so it appears under the agent's name.
        agent_client(_agent_token(auth)).send_message(conversation_id, content, private=is_private)
    except NoAccess as exc:
        return _no_access_response(request, exc.reason, auth)
    except ChatwootError:
        return _view(request, db, auth, conversation_id, flash="Chatwoot rejected the message.", draft=content)
    audit.record(db, auth.user, "panel.private_note" if is_private else "panel.send_message", "conversation", conversation_id, None, {"chars": len(content), "private": is_private}, auth.ip)
    db.commit()
    return _view(request, db, auth, conversation_id, flash="Private note added." if is_private else "Message sent.")


@router.post("/api/conversations/{conversation_id}/memory", response_class=HTMLResponse)
def save_memory(
    request: Request,
    conversation_id: int,
    name: str = Form(""),
    preferred_language: str = Form(""),
    preferences: str = Form(""),
    notes: str = Form(""),
    last_orders: str = Form(""),
    auth: Auth = Depends(require_panel("panel.act")),
    db: Session = Depends(get_db),
):
    try:
        info = verify_access(auth, conversation_id)
    except NoAccess as exc:
        return _no_access_response(request, exc.reason, auth)
    contact_id = info.get("contact_id")
    if not contact_id:
        return _view(request, db, auth, conversation_id, flash="No contact on this conversation.")
    mem = db.get(ContactMemory, contact_id) or ContactMemory(contact_id=contact_id, last_orders=[], summaries=[])
    db.add(mem)
    fields = ["name", "preferred_language", "preferences", "notes", "last_orders"]
    before = audit.model_snapshot(mem, fields)
    mem.name = name.strip()[:255]
    mem.preferred_language = preferred_language.strip()[:16]
    mem.preferences = preferences.strip()[:2000]
    mem.notes = notes.strip()[:4000]
    orders = [line.strip()[:200] for line in last_orders.splitlines() if line.strip()][:5]
    mem.last_orders = [{"ref": "", "summary": o, "date": ""} for o in orders]
    audit.record(db, auth.user, "panel.memory_update", "contact_memory", contact_id, before, audit.model_snapshot(mem, fields), auth.ip)
    db.commit()
    return _view(request, db, auth, conversation_id, flash="Contact memory saved.")
