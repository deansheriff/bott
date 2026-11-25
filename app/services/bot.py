"""Webhook ingestion and the reply pipeline."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope
from ..models import BotTurn, ConversationState, MessageLog, WebhookEvent
from ..security.ratelimit import limiter
from ..utils import truncate, utcnow
from . import settings_store
from .answer import AnswerResult, generate_answer
from .chatwoot import ChatwootError, bot_client
from .copilot import draft_reply, summarize_for_handoff
from .handoff import HandoffDecision, fallback_summary, perform_handoff, post_answer_check, pre_answer_check
from .llm import LLMError
from .memory import profile_for_prompt, summarize_closed_conversation, upsert_contact
from .spend import SpendCapReached, cap_reached

log = logging.getLogger(__name__)

HISTORY_LIMIT = 10
HANDOFF_RACE_GUARD = timedelta(seconds=20)
STATUS_EVENTS = ("conversation_status_changed", "conversation_updated", "conversation_resolved", "conversation_opened")


# ------------------------------------------------------------------ parsing
@dataclass
class IncomingMessage:
    conversation_id: int
    message_id: int | None
    content: str
    contact_id: int | None
    contact_name: str
    contact_phone: str
    status: str | None
    inbox_id: int | None


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def conversation_id_of(payload: dict) -> int | None:
    conv = payload.get("conversation")
    if isinstance(conv, dict):
        # Chatwoot API paths use the conversation display_id.
        return _int(conv.get("display_id")) or _int(conv.get("id"))
    return _int(payload.get("display_id")) or _int(payload.get("id"))


def _sender(payload: dict) -> dict:
    for candidate in (payload.get("sender"), payload.get("contact"), ((payload.get("conversation") or {}).get("meta") or {}).get("sender")):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def sender_type(payload: dict) -> str:
    s = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    t = str(s.get("type") or payload.get("sender_type") or "").lower()
    return {"agentbot": "agent_bot", "agent_bot": "agent_bot", "contact": "contact", "user": "user"}.get(t.replace(" ", ""), t)


def message_direction(payload: dict) -> str:
    mt = payload.get("message_type")
    if mt in ("incoming", 0, "0"):
        return "incoming"
    if mt in ("outgoing", 1, "1", "template", 3, "3"):
        return "outgoing"
    return str(mt or "")


def should_process(payload: dict) -> tuple[bool, str]:
    if payload.get("event") != "message_created":
        return False, "not_message_created"
    if message_direction(payload) != "incoming":
        return False, "not_incoming"
    if payload.get("private") is True:
        return False, "private_note"
    if sender_type(payload) != "contact":
        return False, "sender_not_contact"
    if not str(payload.get("content") or "").strip():
        return False, "empty_content"
    if conversation_id_of(payload) is None:
        return False, "no_conversation_id"
    return True, "ok"


def parse_incoming(payload: dict) -> IncomingMessage:
    sender = _sender(payload)
    conv = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    return IncomingMessage(
        conversation_id=conversation_id_of(payload) or 0,
        message_id=_int(payload.get("id")),
        content=str(payload.get("content") or "")[:4000],
        contact_id=_int(sender.get("id")),
        contact_name=str(sender.get("name") or "")[:255],
        contact_phone=str(sender.get("phone_number") or "")[:32],
        status=conv.get("status") if isinstance(conv.get("status"), str) else None,
        inbox_id=_int(conv.get("inbox_id")) or _int((payload.get("inbox") or {}).get("id")),
    )


# ------------------------------------------------------------------ storage
def log_message(db: Session, conversation_id: int, message_id: int | None, direction: str, stype: str, private: bool, content: str) -> bool:
    """Insert into the local transcript. Returns False if this Chatwoot message id was already stored."""
    if message_id is not None and db.query(MessageLog.id).filter(MessageLog.chatwoot_message_id == message_id).first():
        return False
    try:
        with db.begin_nested():
            db.add(MessageLog(conversation_id=conversation_id, chatwoot_message_id=message_id, direction=direction, sender_type=stype, private=private, content=content[:4000]))
    except IntegrityError:
        return False
    return True


def get_state(db: Session, conversation_id: int, contact_id: int | None = None, inbox_id: int | None = None) -> ConversationState:
    state = db.get(ConversationState, conversation_id)
    if state is None:
        state = ConversationState(conversation_id=conversation_id, contact_id=contact_id, inbox_id=inbox_id, bot_turns=0, bot_state="active")
        db.add(state)
        db.flush()
    if contact_id and not state.contact_id:
        state.contact_id = contact_id
    return state


def load_history(db: Session, conversation_id: int, exclude_message_id: int | None, limit: int = HISTORY_LIMIT) -> list[dict[str, str]]:
    q = db.query(MessageLog).filter(MessageLog.conversation_id == conversation_id, MessageLog.private.is_(False))
    if exclude_message_id is not None:
        q = q.filter((MessageLog.chatwoot_message_id.is_(None)) | (MessageLog.chatwoot_message_id != exclude_message_id))
    rows = q.order_by(MessageLog.created_at.desc(), MessageLog.id.desc()).limit(limit).all()
    return [{"role": "user" if r.direction == "incoming" else "assistant", "content": r.content} for r in reversed(rows) if r.content]


def _apply_status(state: ConversationState, status: str | None) -> None:
    if not status:
        return
    if state.last_status == "resolved" and status != "resolved":
        state.summarized_at = None  # reopened: summarise again at next close
    state.last_status = status


# ------------------------------------------------------------------ ingest
def ingest_event(payload: dict, delivery_id: str | None) -> tuple[str, str, int | None]:
    """Synchronous part of the webhook. Returns (action, reason, conversation_id)."""
    event = str(payload.get("event") or "")[:64]
    conv_id = conversation_id_of(payload)
    s = get_settings()
    with session_scope() as db:
        action, reason = "ignored", "unhandled_event"
        if event == "message_created" and conv_id:
            stype = sender_type(payload)
            direction = message_direction(payload)
            is_new = log_message(db, conv_id, _int(payload.get("id")), direction, stype, payload.get("private") is True, str(payload.get("content") or ""))
            conv = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
            state = get_state(db, conv_id, _int(_sender(payload).get("id")) if stype == "contact" else None, _int(conv.get("inbox_id")))
            _apply_status(state, conv.get("status") if isinstance(conv.get("status"), str) else None)
            state.last_message_at = utcnow()
            ok, reason = should_process(payload)
            if not is_new:
                action, reason = "duplicate", "already_seen"
            elif ok and not limiter.hit(f"conv:{conv_id}", s.webhook_rate_limit_per_minute, 60):
                action, reason = "ignored", "rate_limited"
            elif ok:
                action = "process"
            if state.last_status == "resolved" and state.summarized_at is None and action != "process":
                action = "summarize"
        elif event in STATUS_EVENTS and conv_id:
            state = get_state(db, conv_id)
            status = payload.get("status") if isinstance(payload.get("status"), str) else None
            _apply_status(state, status)
            action, reason = ("summarize", "resolved") if status == "resolved" else ("ignored", "status_recorded")
        db.add(WebhookEvent(delivery_id=(delivery_id or "")[:64] or None, event=event, conversation_id=conv_id, outcome="accepted" if action in ("process", "summarize") else action, reason=reason))
    log.info("webhook.ingested", extra={"event": event, "conversation_id": conv_id, "action": action, "reason": reason})
    return action, reason, conv_id


def summarize_task(conversation_id: int) -> None:
    try:
        with session_scope() as db:
            summarize_closed_conversation(db, conversation_id)
    except Exception:
        log.exception("memory.summarize_task_failed", extra={"conversation_id": conversation_id})


# ------------------------------------------------------------------ decision (shared with the Playground)
SPEND_CAP_REASON = "Daily LLM spend cap reached (handoff-only mode)"


def decide_and_answer(
    db: Session,
    question: str,
    history: list[dict[str, str]],
    profile: dict,
    *,
    contact_phone: str | None,
    contact_id: int | None,
    conversation_id: int | None,
    turns: int,
    bot_turn_id: int | None,
    purpose: str = "answer",
) -> tuple[HandoffDecision | None, AnswerResult | None]:
    """Apply every handoff rule and, if none fires first, ask the model. Returns (decision, answer)."""
    keywords = settings_store.get_setting(db, "handoff_keywords")
    phrases = settings_store.get_setting(db, "human_request_phrases")
    max_turns = int(settings_store.get_setting(db, "max_bot_turns"))
    threshold = float(settings_store.get_setting(db, "confidence_threshold"))

    if cap_reached(db):
        return HandoffDecision(SPEND_CAP_REASON, "spend_cap"), None
    decision = pre_answer_check(question, turns, max_turns, keywords, phrases)
    if decision is not None:
        return decision, None
    try:
        answer = generate_answer(db, question, history, profile, contact_phone=contact_phone, contact_id=contact_id, conversation_id=conversation_id, bot_turn_id=bot_turn_id, purpose=purpose)
    except SpendCapReached:
        return HandoffDecision(SPEND_CAP_REASON, "spend_cap"), None
    except LLMError as exc:
        return HandoffDecision(f"LLM unavailable ({exc})", "error"), None
    return post_answer_check(answer.confidence, answer.needs_human, answer.answered_from_knowledge, answer.unverified_amounts, threshold, answer.reason), answer


def apply_greeting(db: Session, reply: str, turns: int, history: list[dict[str, str]]) -> str:
    """The configured greeting is prefixed to the bot's first reply in a conversation."""
    greeting = (settings_store.get_setting(db, "greeting") or "").strip()
    if greeting and turns == 0 and not any(h["role"] == "assistant" for h in history):
        return f"{greeting}\n\n{reply}"
    return reply


def record_answer(turn: BotTurn, answer: AnswerResult) -> None:
    turn.confidence, turn.chunks, turn.cost_usd, turn.model = answer.confidence, [c.as_dict() for c in answer.chunks], answer.cost_usd, answer.model
    turn.tool_calls = [{"name": t["name"], "args": t["args"], "output": t["output"]} for t in answer.tool_calls]


def prepare_handoff(
    db: Session,
    decision: HandoffDecision,
    answer: AnswerResult | None,
    history: list[dict[str, str]],
    question: str,
    profile: dict,
    *,
    contact_phone: str | None,
    contact_id: int | None,
    conversation_id: int | None,
    contact_name: str,
    turns: int,
) -> tuple[list[str], str | None]:
    """Build the copilot output for a handoff: (3 summary lines, suggested reply or None)."""
    summary_lines = None
    suggestion = None
    if decision.category not in ("spend_cap", "error", "bot_off"):
        try:
            summary_lines = summarize_for_handoff(db, history, question, conversation_id)
        except (SpendCapReached, LLMError):
            pass
        try:
            if answer is not None and answer.answered_from_knowledge and answer.reply and not answer.unverified_amounts:
                suggestion = answer.reply  # already grounded; no second model call
            else:
                suggestion, _ = draft_reply(db, question, history, profile, contact_phone, contact_id, conversation_id)
        except (SpendCapReached, LLMError):
            pass
    return summary_lines or fallback_summary(contact_name, history, question, turns), suggestion or None


# ------------------------------------------------------------------ reply pipeline
def process_incoming(payload: dict) -> None:
    """Background task: decide, answer or hand off."""
    msg = parse_incoming(payload)
    started = time.perf_counter()
    try:
        with session_scope() as db:
            _process(db, msg, started)
    except Exception:
        log.exception("bot.process_failed", extra={"conversation_id": msg.conversation_id, "message_id": msg.message_id})
        with session_scope() as db:
            db.add(WebhookEvent(event="message_created", conversation_id=msg.conversation_id, outcome="error", reason="processing_failed"))


def _process(db: Session, msg: IncomingMessage, started: float) -> None:
    conv_id = msg.conversation_id
    switch = settings_store.get_setting(db, "bot_switch")
    if switch == "PAUSED":
        log.info("bot.paused_skip", extra={"conversation_id": conv_id})
        return

    state = get_state(db, conv_id, msg.contact_id, msg.inbox_id)
    client = bot_client()
    status = msg.status
    if not status:
        try:
            status = client.get_conversation(conv_id).get("status")
        except ChatwootError:
            status = None
    _apply_status(state, status)
    if status != "pending":
        # A human owns this conversation (open/snoozed/resolved): stay silent.
        log.info("bot.silent_not_pending", extra={"conversation_id": conv_id, "status": status})
        return
    if state.bot_state in ("handed_off", "paused"):
        if state.handoff_at and utcnow() - state.handoff_at < HANDOFF_RACE_GUARD:
            return
        # A human set the conversation back to pending: the bot resumes with a fresh turn count.
        state.bot_state, state.bot_turns, state.handoff_reason, state.handoff_category = "active", 0, "", ""

    mem = upsert_contact(db, msg.contact_id, msg.contact_name, msg.contact_phone)
    profile = profile_for_prompt(mem)
    history = load_history(db, conv_id, msg.message_id)
    turn = BotTurn(conversation_id=conv_id, message_id=msg.message_id, question=msg.content, model="")
    db.add(turn)
    db.flush()

    if switch == "OFF":
        client.toggle_status(conv_id, "open")
        state.bot_state, state.handoff_reason, state.handoff_category, state.handoff_at, state.last_status = "handed_off", "Bot switched off", "bot_off", utcnow(), "open"
        turn.handoff, turn.handoff_reason, turn.handoff_category = True, "Bot switched off", "bot_off"
        return

    decision, answer = decide_and_answer(db, msg.content, history, profile, contact_phone=msg.contact_phone, contact_id=msg.contact_id, conversation_id=conv_id, turns=state.bot_turns, bot_turn_id=turn.id)
    if answer is not None:
        record_answer(turn, answer)

    if decision is None and answer is not None:
        reply = apply_greeting(db, answer.reply, state.bot_turns, history)
        sent = client.send_message(conv_id, reply)
        log_message(db, conv_id, _int(sent.get("id")), "outgoing", "agent_bot", False, reply)
        state.bot_turns += 1
        if mem is not None and answer.language and not mem.preferred_language:
            mem.preferred_language = answer.language
        turn.reply = reply
        turn.latency_ms = int((time.perf_counter() - started) * 1000)
        log.info("bot.replied", extra={"conversation_id": conv_id, "turn_id": turn.id, "confidence": answer.confidence, "latency_ms": turn.latency_ms})
        return

    assert decision is not None
    summary_lines, suggestion = prepare_handoff(db, decision, answer, history, msg.content, profile, contact_phone=msg.contact_phone, contact_id=msg.contact_id, conversation_id=conv_id, contact_name=msg.contact_name, turns=state.bot_turns)
    perform_handoff(db, client, state, msg.content, decision, summary_lines, suggestion, settings_store.get_setting(db, "handoff_message"))
    turn.handoff, turn.handoff_reason, turn.handoff_category = True, truncate(decision.reason, 255), decision.category
    turn.unanswered = decision.category in ("no_knowledge", "low_confidence", "unverified_price")
    turn.reply = suggestion or ""
    turn.latency_ms = int((time.perf_counter() - started) * 1000)
