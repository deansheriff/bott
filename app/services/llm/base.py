"""Provider-neutral LLM interface.

Messages use a neutral dict format:
  {"role": "system"|"user"|"assistant"|"tool", "content": str,
   "tool_calls": [ToolCall, ...] (assistant only), "raw": provider payload (assistant only),
   "tool_call_id": str, "name": str (tool only)}
Each provider adapter maps this to its own wire format (see openai_provider.py,
gemini_provider.py). To add Anthropic, implement LLMProvider in anthropic_provider.py
(system prompt -> `system=`, tool results -> `tool_result` content blocks) and register
it in llm/__init__.py.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    # Provider-specific payload to resend unchanged on the next turn (Gemini thought
    # signatures). Callers put it on the assistant message as msg["raw"].
    raw: Any = None


class LLMError(Exception):
    pass


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        json_mode: bool = False,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> LLMResponse: ...
