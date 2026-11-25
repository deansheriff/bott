"""Gemini adapter, provider switching and settings validation (no network)."""
import json
from types import SimpleNamespace

import pytest
from google.genai import types

from app.services import settings_store
from app.services.llm import LLMError, ToolCall, model_matches, resolve_model, set_provider
from app.services.llm.gemini_provider import GeminiProvider
from app.services.llm.pricing import price_for
from app.services.tools import TOOL_SPECS

from .conftest import csrf_from, login, make_user


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        return self.responses.pop(0)


def response(parts, prompt=120, out=30, thoughts=0):
    content = types.Content(role="model", parts=parts)
    usage = SimpleNamespace(prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts)
    return SimpleNamespace(candidates=[SimpleNamespace(content=content)], usage_metadata=usage, model_version="gemini-3.5-flash-lite-001")


def provider_with(responses):
    models = FakeModels(responses)
    return GeminiProvider("", client=SimpleNamespace(models=models)), models


# ------------------------------------------------------------ mapping
def test_messages_mapping():
    p, _ = provider_with([])
    call = ToolCall(id="c1", name="menu_lookup", arguments={"query": "jollof"})
    system, contents = p.to_wire([
        {"role": "system", "content": "RULES"},
        {"role": "assistant", "content": "Welcome!"},
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "price of jollof?"},
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "name": "menu_lookup", "content": json.dumps({"count": 1})},
        {"role": "tool", "tool_call_id": "c2", "name": "opening_status", "content": "not json"},
    ])
    assert system == "RULES"
    roles = [c.role for c in contents]
    assert roles == ["user", "model", "user", "model", "user"]  # starts with user; consecutive users merged
    assert [pt.text for pt in contents[2].parts] == ["hi", "price of jollof?"]
    assert contents[3].parts[0].function_call.name == "menu_lookup"
    responses = contents[4].parts  # both tool results grouped into one turn
    assert [r.function_response.name for r in responses] == ["menu_lookup", "opening_status"]
    assert responses[1].function_response.response == {"result": "not json"}


def test_raw_model_content_is_resent_unchanged():
    p, _ = provider_with([])
    raw = types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="opening_status", args={}), thought_signature=b"sig")])
    _, contents = p.to_wire([{"role": "user", "content": "open?"}, {"role": "assistant", "content": None, "tool_calls": [ToolCall("x", "opening_status", {})], "raw": raw}])
    assert contents[1] is raw and contents[1].parts[0].thought_signature == b"sig"


def test_tool_declarations():
    p, _ = provider_with([])
    decls = {d.name: d for d in p.tools_to_wire(TOOL_SPECS)[0].function_declarations}
    assert set(decls) == {"menu_lookup", "order_status", "delivery_check", "opening_status"}
    assert decls["opening_status"].parameters_json_schema is None  # no-arg tool sends no schema
    assert decls["delivery_check"].parameters_json_schema["required"] == ["area"]


# ------------------------------------------------------------ responses
def test_function_call_parsed_and_raw_kept():
    fc = types.Part(function_call=types.FunctionCall(name="delivery_check", args={"area": "Lekki"}))
    p, models = provider_with([response([fc])])
    r = p.complete([{"role": "user", "content": "lekki?"}], model="gemini-3.5-flash-lite", tools=TOOL_SPECS, json_mode=True)
    assert r.tool_calls[0].name == "delivery_check" and r.tool_calls[0].arguments == {"area": "Lekki"}
    assert r.raw is not None
    cfg = models.requests[0]["config"]
    assert cfg.tools and cfg.response_mime_type is None  # JSON mode not combined with tools


def test_text_excludes_thoughts_and_counts_thinking_tokens():
    parts = [types.Part(text="secret reasoning", thought=True), types.Part(text='{"reply": "Hi"}')]
    p, models = provider_with([response(parts, prompt=100, out=20, thoughts=50)])
    r = p.complete([{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}], model="gemini-3.5-flash-lite", json_mode=True)
    assert r.content == '{"reply": "Hi"}' and r.tool_calls == [] and r.raw is None
    assert (r.input_tokens, r.output_tokens) == (100, 70)
    cfg = models.requests[0]["config"]
    assert cfg.response_mime_type == "application/json" and cfg.system_instruction == "S"
    assert cfg.max_output_tokens > 700  # headroom for thinking


def test_blocked_or_empty_response():
    p, _ = provider_with([SimpleNamespace(candidates=[], usage_metadata=None, model_version=None)])
    r = p.complete([{"role": "user", "content": "x"}], model="gemini-3.5-flash-lite")
    assert r.content is None and r.tool_calls == []


def test_api_error_becomes_llm_error():
    class Boom:
        def generate_content(self, **kw):
            raise RuntimeError("quota")

    p = GeminiProvider("", client=SimpleNamespace(models=Boom()))
    with pytest.raises(LLMError):
        p.complete([{"role": "user", "content": "x"}], model="gemini-3.5-flash-lite")


def test_missing_key_raises():
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiProvider("")


# ------------------------------------------------------------ switching
def test_resolve_model_and_pricing():
    assert resolve_model("gemini", "") == "gemini-3.5-flash-lite"
    assert resolve_model("gemini", "gpt-4o-mini") == "gemini-3.5-flash-lite"
    assert resolve_model("openai", "gemini-3.8-flash") == "gpt-4o-mini"
    assert resolve_model("gemini", "gemini-4-hydrogen") == "gemini-4-hydrogen"
    assert model_matches("openai", "gpt-4.1-mini") and not model_matches("gemini", "gpt-4.1-mini")
    assert price_for("gemini-3.5-flash-lite-001") == (0.30, 2.50)


def test_full_answer_loop_with_gemini(client, db):
    """Tool call round-trip through generate_answer, using Gemini as the active provider."""
    from decimal import Decimal

    from app.models import LLMUsage, MenuItem
    from app.services.answer import generate_answer

    db.add(MenuItem(name="Jollof Rice", category="Mains", price=Decimal(4500)))
    settings_store.set_setting(db, "llm_provider", "gemini")
    settings_store.set_setting(db, "llm_model", "gemini-3.5-flash-lite")
    db.commit()
    fc = types.Part(function_call=types.FunctionCall(name="menu_lookup", args={"query": "jollof"}), thought_signature=b"sig")
    final = types.Part(text='```json\n{"reply": "Jollof Rice is ₦4,500.", "confidence": 0.9, "answered_from_knowledge": true, "needs_human": false, "reason": "menu", "language": "en"}\n```')
    gp, models = provider_with([response([fc]), response([final])])
    set_provider(gp)
    result = generate_answer(db, "How much is jollof?", [], {}, conversation_id=7)
    assert result.reply == "Jollof Rice is ₦4,500." and result.unverified_amounts == []
    second_turn = models.requests[1]["contents"]
    assert any(getattr(pt, "thought_signature", None) == b"sig" for c in second_turn for pt in c.parts)
    usage = db.query(LLMUsage).all()
    assert len(usage) == 2 and all(u.model.startswith("gemini-3.5-flash-lite") for u in usage)


def test_settings_validates_provider_and_model(client, db):
    make_user(db, "a@example.com")
    login(client, "a@example.com")
    token = csrf_from(client.get("/admin/settings").text)
    base = {"csrf_token": token, "daily_spend_cap_usd": "5", "timezone_name": "Africa/Lagos", "handoff_keywords": "refund: refund", "human_request_phrases": "human", "llm_price_overrides": "{}"}
    r = client.post("/admin/settings", data={**base, "llm_provider": "gemini", "llm_model": "gpt-4o-mini"})
    assert r.status_code == 400 and "belong to gemini" in r.text
    r = client.post("/admin/settings", data={**base, "llm_provider": "gemini", "llm_model": ""})
    assert r.status_code == 200 and "Settings saved" in r.text
    db.expire_all()
    assert settings_store.get_setting(db, "llm_provider") == "gemini"
    assert settings_store.get_setting(db, "llm_model") == "gemini-3.5-flash-lite"
    assert "GEMINI_API_KEY" in client.get("/admin/settings").text
