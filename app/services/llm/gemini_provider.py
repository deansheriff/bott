"""Google Gemini adapter (google-genai SDK, generateContent API).

Mapping from the neutral message format:
  system            -> config.system_instruction
  user              -> Content(role="user")
  assistant         -> Content(role="model"); when the model made function calls we resend
                       its ORIGINAL Content (msg["raw"]) so thought signatures are preserved,
                       which Gemini requires in multi-turn function calling
  tool (consecutive)-> one Content(role="user") holding all function_response parts
JSON mode is only requested when no tools are passed (function calling combined with a
JSON response MIME type is not documented as supported); with tools, the system prompt
asks for JSON and answer.parse_model_json extracts it.
"""
from __future__ import annotations

import json
from typing import Any

from .base import LLMError, LLMProvider, LLMResponse, ToolCall, ToolSpec

# Thinking tokens count against max_output_tokens on Gemini; leave headroom so the
# visible answer isn't truncated. Only tokens actually produced are billed.
THINKING_HEADROOM = 1024


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, timeout: float = 30.0, client: Any = None) -> None:
        from google.genai import types

        self.types = types
        if client is not None:
            self._client = client
            return
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not configured")
        from google import genai

        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout * 1000)))

    # ------------------------------------------------------------ request mapping
    def tools_to_wire(self, tools: list[ToolSpec]) -> list[Any]:
        t = self.types
        decls = []
        for spec in tools:
            kwargs: dict[str, Any] = {"name": spec.name, "description": spec.description}
            if (spec.parameters or {}).get("properties"):
                kwargs["parameters_json_schema"] = spec.parameters
            decls.append(t.FunctionDeclaration(**kwargs))
        return [t.Tool(function_declarations=decls)]

    def to_wire(self, messages: list[dict[str, Any]]) -> tuple[str, list[Any]]:
        t = self.types
        system: list[str] = []
        contents: list[Any] = []
        responses: list[Any] = []

        def flush_responses() -> None:
            if responses:
                contents.append(t.Content(role="user", parts=list(responses)))
                responses.clear()

        def add_text(role: str, text: str) -> None:
            last = contents[-1] if contents else None
            # Merge consecutive plain-text turns of the same role (e.g. two customer messages).
            if last is not None and last.role == role and all(p.text is not None for p in (last.parts or [])):
                last.parts.append(t.Part(text=text))
            else:
                contents.append(t.Content(role=role, parts=[t.Part(text=text)]))

        for m in messages:
            role = m["role"]
            if role == "system":
                system.append(m.get("content") or "")
            elif role == "tool":
                try:
                    payload = json.loads(m.get("content") or "{}")
                except json.JSONDecodeError:
                    payload = {"result": m.get("content")}
                if not isinstance(payload, dict):
                    payload = {"result": payload}
                responses.append(t.Part.from_function_response(name=m.get("name") or "tool", response=payload))
            else:
                flush_responses()
                if role == "assistant":
                    raw = m.get("raw")
                    if isinstance(raw, t.Content):
                        contents.append(raw)
                    elif m.get("tool_calls"):
                        parts = [t.Part(text=m["content"])] if m.get("content") else []
                        parts += [t.Part(function_call=t.FunctionCall(name=tc.name, args=tc.arguments)) for tc in m["tool_calls"]]
                        contents.append(t.Content(role="model", parts=parts))
                    elif m.get("content"):
                        add_text("model", m["content"])
                elif m.get("content"):
                    add_text("user", m["content"])
        flush_responses()
        if contents and contents[0].role != "user":
            contents.insert(0, t.Content(role="user", parts=[t.Part(text="(earlier in the conversation)")]))
        return "\n\n".join(system), contents

    # ------------------------------------------------------------ call
    def complete(self, messages, *, model, tools=None, json_mode=False, max_tokens=700, temperature=0.2) -> LLMResponse:
        t = self.types
        system, contents = self.to_wire(messages)
        config: dict[str, Any] = {"temperature": temperature, "max_output_tokens": max_tokens + THINKING_HEADROOM}
        if system:
            config["system_instruction"] = system
        if tools:
            config["tools"] = self.tools_to_wire(tools)
        elif json_mode:
            config["response_mime_type"] = "application/json"
        try:
            resp = self._client.models.generate_content(model=model, contents=contents, config=t.GenerateContentConfig(**config))
        except Exception as exc:  # google.genai.errors.APIError and network errors
            raise LLMError(type(exc).__name__) from exc

        candidates = getattr(resp, "candidates", None) or []
        content = candidates[0].content if candidates and getattr(candidates[0], "content", None) else None
        parts = list(getattr(content, "parts", None) or [])
        text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
        calls = []
        for i, p in enumerate(parts):
            fc = getattr(p, "function_call", None)
            if fc is not None and getattr(fc, "name", None):
                calls.append(ToolCall(id=getattr(fc, "id", None) or f"call_{i}", name=fc.name, arguments=dict(getattr(fc, "args", None) or {})))
        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        # Thinking tokens are billed as output.
        out_tok = (getattr(usage, "candidates_token_count", 0) or 0) + (getattr(usage, "thoughts_token_count", 0) or 0)
        return LLMResponse(
            content=text or None,
            tool_calls=calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            model=getattr(resp, "model_version", None) or model,
            raw=content if calls else None,
        )
