from __future__ import annotations

from ...config import get_settings
from .base import LLMError, LLMProvider, LLMResponse, ToolCall, ToolSpec

_provider: LLMProvider | None = None


def get_provider() -> LLMProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        if s.llm_provider == "openai":
            from .openai_provider import OpenAIProvider

            _provider = OpenAIProvider(s.openai_api_key)
        elif s.llm_provider == "fake":
            from .fake import FakeProvider

            _provider = FakeProvider()
        else:
            raise LLMError(f"Unknown LLM_PROVIDER {s.llm_provider!r}")
    return _provider


def set_provider(provider: LLMProvider | None) -> None:
    global _provider
    _provider = provider


__all__ = ["LLMError", "LLMProvider", "LLMResponse", "ToolCall", "ToolSpec", "get_provider", "set_provider"]
