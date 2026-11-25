"""Handoff rules (pure functions) and the handoff action."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import ConversationState
from ..utils import truncate, utcnow
from .chatwoot import ChatwootClient
from .settings_store import RED_FLAG_CATEGORIES

log = logging.getLogger(__name__)


@dataclass
class HandoffDecision:
    reason: str
    category: str  # refund | complaint | allergy | food_poisoning | legal | human_request | low_confidence | turn_limit | no_knowledge | unverified_price | spend_cap | bot_off | error

    @property
    def red_flag(self) -> bool:
        return self.category in RED_FLAG_CATEGORIES


def _phrase_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in phrase.lower().split()) + r"(?!\w)")


def match_keywords(text: str, keywords: dict[str, list[str]]) -> tuple[str, str] | None:
    """Return (category, matched phrase) for the first sensitive keyword found."""
    lowered = (text or "").lower()
    for category, phrases in keywords.items():
        for phrase in phrases:
            if phrase.strip() and _phrase_re(phrase).search(lowered):
                return category, phrase
    return None


def wants_human(text: str, phrases: list[str]) -> bool:
    lowered = (text or "").lower()
    return any(p.strip() and _phrase_re(p).search(lowered) for p in phrases)


def pre_answer_check(text: str, turns: int, max_turns: int, keywords: dict[str, list[str]], human_phrases: list[str]) -> HandoffDecision | None:
    """Rules that apply before calling the model."""
    hit = match_keywords(text, keywords)
    if hit:
        category, phrase = hit
        return HandoffDecision(f"Sensitive topic ({category.replace('_', ' ')}): mentioned '{phrase}'", category)
    if wants_human(text, human_phrases):
        return HandoffDecision("Customer asked for a human", "human_request")
    if turns >= max_turns:
        return HandoffDecision(f"Bot reached {max_turns} turns without resolution", "turn_limit")
    return None


def post_answer_check(confidence: float, needs_human: bool, answered_from_knowledge: bool, unverified_amounts: list[str], threshold: float, model_reason: str = "") -> HandoffDecision | None:
    """Rules that apply to the model's answer."""
    if unverified_amounts:
        return HandoffDecision(f"Reply contained a price not found in tools/knowledge ({', '.join(unverified_amounts[:3])})", "unverified_price")
    if needs_human:
        return HandoffDecision(truncate(f"Model could not answer from knowledge: {model_reason}" if model_reason else "Model could not answer from knowledge", 250), "no_knowledge")
    if confidence < threshold:
        return HandoffDecision(f"Low confidence ({confidence:.2f} < {threshold:.2f})", "low_confidence")
    return None


def build_handoff_note(summary_lines: list[str], question: str, decision: HandoffDecision) -> str:
    lines = (summary_lines + ["", "", ""])[:3]
    flag = "🚩 " if decision.red_flag else ""
    return (
        f"{flag}Bot handoff\n"
        f"Summary:\n- {truncate(lines[0], 200)}\n- {truncate(lines[1], 200)}\n- {truncate(lines[2], 200)}\n"
        f"Customer question: {truncate(question, 500)}\n"
        f"Escalation reason: {decision.reason}"
    )


def build_suggestion_note(suggested_reply: str) -> str:
    return f"💡 Suggested reply (drafted from the knowledge base; review before sending):\n{suggested_reply}"


def perform_handoff(
    db: Session,
    client: ChatwootClient,
    state: ConversationState,
    question: str,
    decision: HandoffDecision,
    summary_lines: list[str],
    suggested_reply: str | None,
    customer_message: str | None,
) -> None:
    """Tell the customer (optional), set status open, then post the private note(s)."""
    conv_id = state.conversation_id
    if customer_message:
        client.send_message(conv_id, customer_message)
    client.toggle_status(conv_id, "open")
    client.send_message(conv_id, build_handoff_note(summary_lines, question, decision), private=True)
    if suggested_reply:
        client.send_message(conv_id, build_suggestion_note(suggested_reply), private=True)
    state.bot_state = "handed_off"
    state.handoff_reason = truncate(decision.reason, 255)
    state.handoff_category = decision.category
    state.handoff_at = utcnow()
    state.last_status = "open"
    db.commit()
    log.info("bot.handoff", extra={"conversation_id": conv_id, "category": decision.category})


def fallback_summary(contact_name: str, history: list[dict], question: str, turns: int) -> list[str]:
    last_customer = [h["content"] for h in history if h["role"] == "user"][-2:]
    return [
        f"Customer {contact_name or '(no name)'}; bot replied {turns} time(s).",
        f"Earlier: {truncate(' / '.join(last_customer), 160) or 'no earlier messages'}",
        f"Latest: {truncate(question, 160)}",
    ]


def parse_summary(content: str | None) -> list[str] | None:
    try:
        data = json.loads(content or "")
        lines = [str(x) for x in data.get("lines", [])][:3]
        return lines if len(lines) == 3 else None
    except (json.JSONDecodeError, AttributeError):
        return None
