from __future__ import annotations

import json
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, ToolCall, ToolSpec


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, timeout: float = 30.0) -> None:
        from openai import OpenAI

        if not api_key:
            raise LLMError("OPENAI_API_KEY is not configured")
        self._client = OpenAI(api_key=api_key, timeout=timeout, max_retries=2)

    @staticmethod
    def _to_wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:  # m["raw"] is ignored: OpenAI needs nothing extra between turns
            role = m["role"]
            if role == "assistant" and m.get("tool_calls"):
                out.append(
                    {
                        "role": "assistant",
                        "content": m.get("content") or None,
                        "tool_calls": [
                            {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                            for tc in m["tool_calls"]
                        ],
                    }
                )
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
            else:
                out.append({"role": role, "content": m.get("content") or ""})
        return out

    def complete(self, messages, *, model, tools=None, json_mode=False, max_tokens=700, temperature=0.2) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_wire(messages),
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # openai.APIError and network errors
            raise LLMError(type(exc).__name__) from exc
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args if isinstance(args, dict) else {}))
        usage = resp.usage
        return LLMResponse(
            content=msg.content,
            tool_calls=calls,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=resp.model or model,
        )
