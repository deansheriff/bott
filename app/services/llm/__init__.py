from __future__ import annotations

import logging
import threading

from ...config import get_settings
from .base import LLMError, LLMProvider, LLMResponse, ToolCall, ToolSpec

log = logging.getLogger(__name__)

PROVIDERS = ("openai", "gemini", "fake")
DEFAULT_MODELS = {"openai": "gpt-4o-mini", "gemini": "gemini-3.5-flash-lite", "fake": "fake-model"}

_providers: dict[str, LLMProvider] = {}
_override: LLMProvider | None = None
_lock = threading.Lock()


def model_matches(provider: str, model: str) -> bool:
    is_gemini = (model or "").startswith("gemini")
    if provider == "gemini":
        return is_gemini
    if provider == "openai":
        return bool(model) and not is_gemini
    return True


def resolve_model(provider: str, model: str | None) -> str:
    """Use the configured model if it belongs to the provider, else the provider default."""
    if model and model_matches(provider, model):
        return model
    default = DEFAULT_MODELS.get(provider, "")
    if model:
        log.warning("llm.model_provider_mismatch", extra={"provider": provider, "model": model, "using": default})
    return default


def _build(name: str) -> LLMProvider:
    s = get_settings()
    if name == "openai":
        from .openai_provider import OpenAIProvider

        return OpenAIProvider(s.openai_api_key)
    if name == "gemini":
        from .gemini_provider import GeminiProvider

        return GeminiProvider(s.gemini_api_key)
    if name == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    raise LLMError(f"Unknown LLM provider {name!r}")


def get_provider(name: str | None = None) -> LLMProvider:
    if _override is not None:
        return _override
    name = name or get_settings().llm_provider
    with _lock:
        if name not in _providers:
            _providers[name] = _build(name)
        return _providers[name]


def key_configured(name: str) -> bool:
    s = get_settings()
    return {"openai": bool(s.openai_api_key), "gemini": bool(s.gemini_api_key), "fake": True}.get(name, False)


def set_provider(provider: LLMProvider | None) -> None:
    """Pin one provider for every call (tests)."""
    global _override
    _override = provider
    _providers.clear()


__all__ = [
    "DEFAULT_MODELS", "PROVIDERS", "LLMError", "LLMProvider", "LLMResponse", "ToolCall", "ToolSpec",
    "get_provider", "key_configured", "model_matches", "resolve_model", "set_provider",
]
