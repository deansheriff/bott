"""Provider-neutral LLM interface.

Messages use a neutral dict format:
  {"role": "system"|"user"|"assistant"|"tool", "content": str,
   "tool_calls": [ToolCall, ...] (assistant only), "tool_call_id": str, "name": str (tool only)}
Each provider adapter maps this to its own wire format. To add Anthropic,
implement LLMProvider in anthropic_provider.py (system prompt -> `system=`,
tool results -> `tool_result` content blocks) and register it in registry.py.
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
