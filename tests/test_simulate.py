"""Playground "Simulate handoff": transcript parsing and parity with the live pipeline, with no Chatwoot calls."""
import json

import pytest

from app.models import BotTurn
from app.services import settings_store
from app.services.llm import LLMResponse
from app.services.simulate import TranscriptError, parse_transcript

from .conftest import csrf_from, login, make_user


def smart_responder(reply="We deliver to Lekki Phase 1.", confidence=0.9, from_knowledge=True):
    """Return summary lines for summary prompts and an answer for everything else."""

    def responder(messages, opts):
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        if "exactly 3 short lines" in system:
            body = {"lines": ["Customer asked about delivery to Lekki.", "Bot confirmed Lekki Phase 1.", "Customer now wants a refund for a cold order."]}
        else:
            body = {"reply": reply, "confidence": confidence, "answered_from_knowledge": from_knowledge, "needs_human": False, "reason": "test", "language": "en"}
        return LLMResponse(content=json.dumps(body), input_tokens=40, output_tokens=20, model=opts["model"])

    return responder


# ------------------------------------------------------------ parsing
def test_parse_transcript_roles_and_continuations():
    msgs = parse_transcript("Customer: Hi\nB: Hello! How can I help?\nC: Do you deliver\nto Lekki?\n\nagent - sure\ncust> thanks")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    assert msgs[2]["content"] == "Do you deliver\nto Lekki?"
    assert msgs[3]["content"] == "sure" and msgs[4]["content"] == "thanks"
    with pytest.raises(TranscriptError, match="End with a customer"):
        parse_transcript("Customer: hi\nBot: hello")


def test_parse_transcript_plain_text_is_customer():
    assert parse_transcript("Do you open on Sunday?") == [{"role": "user", "content": "Do you open on Sunday?"}]


def test_parse_transcript_limits():
    with pytest.raises(TranscriptError, match="at least one"):
        parse_transcript("   \n Bot:   ")
    with pytest.raises(TranscriptError, match="at most"):
        parse_transcript("\n".join(f"Customer: q{i}\nBot: a{i}" for i in range(11)) + "\nCustomer: last")
    with pytest.raises(TranscriptError, match="too long"):
        parse_transcript("Customer: " + "x" * 9000)


# ------------------------------------------------------------ endpoint
def _admin(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    return csrf_from(client.get("/admin/playground").text)


def test_refund_simulation_shows_notes_without_chatwoot(client, db, fake_cw, fake_llm):
    fake_llm.responder = smart_responder()
    token = _admin(client, db)
    transcript = "Customer: Hi, do you deliver to Lekki?\nBot: Yes, we deliver to Lekki Phase 1.\nCustomer: My order came cold and I want a refund"
    r = client.post("/admin/playground/simulate", data={"transcript": transcript, "contact_name": "Ada"}, headers={"X-CSRF-Token": token})
    assert r.status_code == 200
    html = r.text
    assert "Hands off to a human" in html and "🚩" in html
    assert "Customer asked about delivery to Lekki." in html  # AI summary line
    assert "Escalation reason" in html and "Customer question: My order came cold" in html
    assert "Suggested reply" in html
    assert settings_store.get_setting(db, "handoff_message")[:20] in html
    assert fake_cw.calls == []  # nothing sent to Chatwoot
    turn = db.query(BotTurn).one()
    assert turn.dry_run and turn.handoff and turn.handoff_category == "refund"


def test_confident_answer_replies_with_greeting(client, db, fake_cw, fake_llm):
    fake_llm.responder = smart_responder("We deliver to Lekki Phase 1.", 0.95)
    token = _admin(client, db)
    r = client.post("/admin/playground/simulate", data={"transcript": "Customer: Do you deliver to Lekki?"}, headers={"X-CSRF-Token": token})
    assert "The bot answers by itself" in r.text
    assert "Hi! I&#39;m the virtual assistant" in r.text and "We deliver to Lekki Phase 1." in r.text
    assert "Private note" not in r.text and fake_cw.calls == []


def test_force_handoff_reuses_grounded_answer_as_suggestion(client, db, fake_cw, fake_llm):
    fake_llm.responder = smart_responder("We deliver to Lekki Phase 1.", 0.95)
    token = _admin(client, db)
    r = client.post("/admin/playground/simulate", data={"transcript": "C: Do you deliver to Lekki?", "mode": "force"}, headers={"X-CSRF-Token": token})
    assert "Hands off to a human" in r.text and "forced" in r.text
    assert "Suggested reply (drafted from the knowledge base" in r.text and "We deliver to Lekki Phase 1." in r.text


def test_turn_limit_counts_bot_lines(client, db, fake_cw, fake_llm):
    token = _admin(client, db)
    transcript = "\n".join(f"Customer: question {i}\nBot: answer {i}" for i in range(6)) + "\nCustomer: one more?"
    r = client.post("/admin/playground/simulate", data={"transcript": transcript}, headers={"X-CSRF-Token": token})
    assert "turn_limit" in r.text and "6 bot turn(s)" in r.text


def test_spend_cap_uses_fallback_summary(client, db, fake_cw, fake_llm):
    token = _admin(client, db)
    settings_store.set_setting(db, "daily_spend_cap_usd", 0)
    db.commit()
    r = client.post("/admin/playground/simulate", data={"transcript": "Customer: what time do you open?"}, headers={"X-CSRF-Token": token})
    assert "spend_cap" in r.text and "No AI calls in this mode" in r.text
    assert fake_llm.calls == []


def test_customer_text_is_escaped(client, db, fake_cw, fake_llm):
    token = _admin(client, db)
    r = client.post("/admin/playground/simulate", data={"transcript": "Customer: <script>alert(1)</script> I want a refund", "mode": "force"}, headers={"X-CSRF-Token": token})
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_bad_transcript_and_access_rules(client, db, fake_cw, fake_llm):
    token = _admin(client, db)
    r = client.post("/admin/playground/simulate", data={"transcript": "Bot: hello"}, headers={"X-CSRF-Token": token})
    assert "End with a customer message" in r.text
    assert client.post("/admin/playground/simulate", data={"transcript": "C: hi"}).status_code == 403  # no CSRF
    client.cookies.clear()
    make_user(db, "staff@example.com", role="staff")
    login(client, "staff@example.com")
    staff_token = csrf_from(client.get("/admin/menu").text)
    assert client.post("/admin/playground/simulate", data={"transcript": "C: hi"}, headers={"X-CSRF-Token": staff_token}).status_code == 403
