"""Playground "Simulate handoff": run a made-up conversation through the real bot logic.

Uses exactly the same functions as the live webhook pipeline (decide_and_answer,
prepare_handoff, build_handoff_note, build_suggestion_note) but never calls Chatwoot.
LLM calls are real and count toward the daily spend cap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..models import BotTurn
from . import settings_store
from .answer import AnswerResult
from .bot import apply_greeting, decide_and_answer, prepare_handoff, record_answer
from .handoff import HandoffDecision, build_handoff_note, build_suggestion_note

MAX_MESSAGES = 20
MAX_CHARS = 8000
_PREFIX_RE = re.compile(r"^\s*(customer|cust|c|user|bot|b|agent|restaurant)\s*[:>\-]\s*(.*)$", re.IGNORECASE)
_ROLES = {"customer": "user", "cust": "user", "c": "user", "user": "user", "bot": "assistant", "b": "assistant", "agent": "assistant", "restaurant": "assistant"}


class TranscriptError(ValueError):
    pass


def parse_transcript(text: str) -> list[dict[str, str]]:
    """Lines start with "Customer:" or "Bot:" (also C:/B:/Agent:). Unprefixed lines continue the previous message."""
    if len(text) > MAX_CHARS:
        raise TranscriptError(f"The conversation is too long (max {MAX_CHARS:,} characters).")
    msgs: list[dict[str, str]] = []
    for line in text.splitlines():
        m = _PREFIX_RE.match(line)
        if m:
            msgs.append({"role": _ROLES[m.group(1).lower()], "content": m.group(2).strip()})
        elif line.strip():
            if msgs:
                msgs[-1]["content"] = (msgs[-1]["content"] + "\n" + line.strip()).strip()
            else:
                msgs.append({"role": "user", "content": line.strip()})
    msgs = [m for m in msgs if m["content"]]
    if not msgs:
        raise TranscriptError("Type at least one customer message.")
    if len(msgs) > MAX_MESSAGES:
        raise TranscriptError(f"Use at most {MAX_MESSAGES} messages.")
    if msgs[-1]["role"] != "user":
        raise TranscriptError("End with a customer message: that's the message the bot reacts to.")
    return msgs


@dataclass
class SimulationResult:
    question: str
    history: list[dict[str, str]]
    bot_turns: int
    decision: HandoffDecision | None
    answer: AnswerResult | None
    forced: bool = False
    reply_sent: str = ""
    customer_message: str = ""
    summary_lines: list[str] = field(default_factory=list)
    suggestion: str | None = None
    handoff_note: str = ""
    suggestion_note: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def handed_off(self) -> bool:
        return self.decision is not None


def simulate(db: Session, transcript: str, *, phone: str | None = None, contact_name: str = "", force_handoff: bool = False) -> SimulationResult:
    msgs = parse_transcript(transcript)
    history, question = msgs[:-1], msgs[-1]["content"]
    turns = sum(1 for m in history if m["role"] == "assistant")
    profile: dict[str, Any] = {"name": contact_name} if contact_name else {}

    turn = BotTurn(question=question, dry_run=True)
    db.add(turn)
    db.flush()
    decision, answer = decide_and_answer(db, question, history, profile, contact_phone=phone, contact_id=None, conversation_id=None, turns=turns, bot_turn_id=turn.id, purpose="playground")
    if answer is not None:
        record_answer(turn, answer)
        turn.reply = answer.reply

    result = SimulationResult(question=question, history=history, bot_turns=turns, decision=decision, answer=answer)
    if decision is None and force_handoff:
        decision = HandoffDecision("Simulated handoff (forced in the Playground)", "human_request")
        result.decision, result.forced = decision, True
        result.notes.append("The bot would normally have replied; the handoff was forced so you can see the copilot output.")

    if decision is None and answer is not None:
        result.reply_sent = apply_greeting(db, answer.reply, turns, history)
    elif decision is not None:
        summary_lines, suggestion = prepare_handoff(db, decision, answer, history, question, profile, contact_phone=phone, contact_id=None, conversation_id=None, contact_name=contact_name, turns=turns)
        result.summary_lines, result.suggestion = summary_lines, suggestion
        result.customer_message = settings_store.get_setting(db, "handoff_message")
        result.handoff_note = build_handoff_note(summary_lines, question, decision)
        result.suggestion_note = build_suggestion_note(suggestion) if suggestion else ""
        if decision.category in ("spend_cap", "error"):
            result.notes.append("No AI calls in this mode: the summary is the plain fallback and there is no suggested reply.")
        elif not suggestion:
            result.notes.append("No suggested reply: the knowledge base and tools don't cover this question (or the draft quoted an unverified price).")
        turn.handoff, turn.handoff_reason, turn.handoff_category = True, decision.reason[:255], decision.category
        turn.reply = suggestion or ""
    db.commit()
    return result
