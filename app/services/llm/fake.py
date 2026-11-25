"""Deterministic provider for tests and offline development."""
from __future__ import annotations

import json
from typing import Any, Callable

from .base import LLMProvider, LLMResponse


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, responder: Callable[[list[dict[str, Any]], dict[str, Any]], LLMResponse] | None = None) -> None:
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    def complete(self, messages, *, model, tools=None, json_mode=False, max_tokens=700, temperature=0.2) -> LLMResponse:
        opts = {"model": model, "tools": tools, "json_mode": json_mode}
        self.calls.append({"messages": messages, **opts})
        if self.responder:
            return self.responder(messages, opts)
        # Default: low-confidence answer, which forces a handoff.
        body = {"reply": "I'm not sure about that.", "confidence": 0.1, "answered_from_knowledge": False, "needs_human": True, "reason": "fake provider", "language": "en", "lines": ["Fake summary line 1", "Fake summary line 2", "Fake summary line 3"], "translation": "[translated]", "summary": "Fake summary"}
        return LLMResponse(content=json.dumps(body), input_tokens=100, output_tokens=50, model=model)
