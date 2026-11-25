from app.models import BotTurn, ConversationState
from app.services import settings_store
from app.services.handoff import build_handoff_note, match_keywords, post_answer_check, pre_answer_check, wants_human
from app.services.settings_store import DEFAULT_HANDOFF_KEYWORDS, DEFAULT_HUMAN_PHRASES

from .conftest import answer_responder, message_payload, post_webhook

KW, HP = DEFAULT_HANDOFF_KEYWORDS, DEFAULT_HUMAN_PHRASES


# ------------------------------------------------------------ pure rules
def test_keyword_categories():
    assert match_keywords("I want a REFUND now", KW)[0] == "refund"
    assert match_keywords("I'm allergic to nuts", KW)[0] == "allergy"
    assert match_keywords("I think I got food poisoning", KW)[0] == "food_poisoning"
    assert match_keywords("I will sue you", KW)[0] == "legal"
    assert match_keywords("I want to make a complaint", KW)[0] == "complaint"


def test_keywords_respect_word_boundaries():
    assert match_keywords("There is an issue with pursuing my order", KW) is None
    assert match_keywords("Do you have jollof rice?", KW) is None


def test_human_request():
    assert wants_human("Can I talk to a real person please", HP)
    assert wants_human("let me speak to someone", HP)
    assert not wants_human("the food was humane", HP)


def test_pre_answer_turn_limit():
    assert pre_answer_check("what time?", 5, 6, KW, HP) is None
    d = pre_answer_check("what time?", 6, 6, KW, HP)
    assert d.category == "turn_limit"


def test_post_answer_rules():
    assert post_answer_check(0.9, False, True, [], 0.6) is None
    assert post_answer_check(0.4, False, True, [], 0.6).category == "low_confidence"
    assert post_answer_check(0.9, True, False, [], 0.6).category == "no_knowledge"
    assert post_answer_check(0.9, False, True, ["₦9,999"], 0.6).category == "unverified_price"


def test_note_has_summary_question_reason():
    d = pre_answer_check("I need a refund", 0, 6, KW, HP)
    note = build_handoff_note(["a", "b", "c"], "I need a refund", d)
    assert "🚩" in note and "- a\n- b\n- c" in note
    assert "Customer question: I need a refund" in note and "Escalation reason:" in note


# ------------------------------------------------------------ pipeline
def test_confident_answer_is_sent(client, fake_cw, fake_llm):
    fake_llm.responder = answer_responder("We open at 10:00.", 0.95)
    assert post_webhook(client, message_payload("What time do you open?")).status_code == 200
    sends = [c for k, c in fake_cw.calls if k == "send"]
    assert len(sends) == 1 and not sends[0]["private"]
    assert "We open at 10:00." in sends[0]["content"]
    assert sends[0]["token"] == "bot-token"
    assert "toggle" not in fake_cw.kinds()


def test_refund_hands_off_with_note(client, fake_cw, db):
    post_webhook(client, message_payload("I want a refund for my order"))
    kinds = fake_cw.kinds()
    assert "toggle" in kinds
    toggle_idx = kinds.index("toggle")
    assert fake_cw.calls[toggle_idx][1]["status"] == "open"
    notes = [(i, c) for i, (k, c) in enumerate(fake_cw.calls) if k == "send" and c["private"]]
    assert notes and notes[0][0] > toggle_idx, "private note must follow the status change"
    assert "Escalation reason" in notes[0][1]["content"] and "refund" in notes[0][1]["content"].lower()
    state = db.get(ConversationState, 42)
    assert state.bot_state == "handed_off" and state.handoff_category == "refund"


def test_bot_silent_when_not_pending(client, fake_cw):
    post_webhook(client, message_payload("hello?", status="open"))
    assert fake_cw.calls == []


def test_ignores_agent_bot_private_and_outgoing(client, fake_cw):
    post_webhook(client, message_payload("agent reply", msg_id=2, message_type="outgoing", sender_type="user"))
    post_webhook(client, message_payload("bot reply", msg_id=3, message_type="outgoing", sender_type="agent_bot"))
    post_webhook(client, message_payload("note", msg_id=4, private=True, sender_type="user"))
    post_webhook(client, message_payload("weird", msg_id=5, sender_type="user"))
    assert fake_cw.calls == []


def test_duplicate_delivery_processed_once(client, fake_cw, fake_llm):
    fake_llm.responder = answer_responder("We open at 10:00.", 0.95)
    post_webhook(client, message_payload("What time do you open?", msg_id=9))
    post_webhook(client, message_payload("What time do you open?", msg_id=9))
    assert fake_cw.kinds().count("send") == 1


def test_low_confidence_hands_off(client, fake_cw, fake_llm, db):
    fake_llm.responder = answer_responder("Maybe?", 0.2)
    post_webhook(client, message_payload("Do you cater weddings?"))
    assert "toggle" in fake_cw.kinds()
    turn = db.query(BotTurn).filter(BotTurn.conversation_id == 42).one()
    assert turn.handoff and turn.unanswered and turn.handoff_category == "low_confidence"


def test_invented_price_hands_off(client, fake_cw, fake_llm, db):
    fake_llm.responder = answer_responder("Jollof rice costs ₦9,999.", 0.99)
    post_webhook(client, message_payload("How much is jollof?"))
    assert "toggle" in fake_cw.kinds()
    customer_msgs = [c["content"] for k, c in fake_cw.calls if k == "send" and not c["private"]]
    assert all("9,999" not in m for m in customer_msgs)
    assert db.query(BotTurn).one().handoff_category == "unverified_price"


def test_turn_limit_hands_off(client, fake_cw, fake_llm, db):
    db.add(ConversationState(conversation_id=42, bot_turns=6, bot_state="active"))
    db.commit()
    post_webhook(client, message_payload("and another question"))
    assert "toggle" in fake_cw.kinds()
    assert db.query(BotTurn).one().handoff_category == "turn_limit"


def test_spend_cap_hands_off_without_llm(client, fake_cw, fake_llm, db):
    settings_store.set_setting(db, "daily_spend_cap_usd", 0)
    db.commit()
    post_webhook(client, message_payload("What time do you open?"))
    assert fake_llm.calls == []
    assert "toggle" in fake_cw.kinds()


def test_bot_off_opens_without_reply(client, fake_cw, fake_llm, db):
    settings_store.set_setting(db, "bot_switch", "OFF")
    db.commit()
    post_webhook(client, message_payload("hi"))
    assert fake_cw.kinds() == ["toggle"] and fake_llm.calls == []


def test_bot_paused_does_nothing(client, fake_cw, fake_llm, db):
    settings_store.set_setting(db, "bot_switch", "PAUSED")
    db.commit()
    post_webhook(client, message_payload("hi"))
    assert fake_cw.calls == [] and fake_llm.calls == []


def test_resumes_after_human_sets_pending(client, fake_cw, fake_llm, db):
    from datetime import timedelta

    from app.utils import utcnow

    db.add(ConversationState(conversation_id=42, bot_turns=3, bot_state="handed_off", handoff_at=utcnow() - timedelta(minutes=5), handoff_reason="x"))
    db.commit()
    fake_llm.responder = answer_responder("We open at 10:00.", 0.95)
    post_webhook(client, message_payload("thanks, what time do you open?", status="pending"))
    assert fake_cw.kinds().count("send") == 1
    db.expire_all()
    state = db.get(ConversationState, 42)
    assert state.bot_state == "active" and state.bot_turns == 1
