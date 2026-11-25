"""USD per 1M tokens (input, output). VERIFY against your provider's current price list;
override per model on /admin/settings ("LLM price overrides").

Gemini prices are paid-tier Standard from Google's pricing page (read 2026-11-25); output
includes thinking tokens. Where Google announced a price rise (Dec 2026 / Jan 2027) the
higher price is used, so the spend cap errs on the safe side.
"""
from __future__ import annotations

DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.8-flash": (1.50, 7.50),
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-4-neon": (3.00, 15.00),
    "gemini-4-carbon": (4.00, 20.00),
    "gemini-4-argon": (4.00, 20.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
FALLBACK_PRICE = (2.50, 10.00)  # deliberately pessimistic for unknown models


def price_for(model: str, overrides: dict | None = None) -> tuple[float, float]:
    overrides = overrides or {}
    if model in overrides:
        pin, pout = overrides[model]
        return float(pin), float(pout)
    # Match dated snapshots such as gpt-4o-mini-2024-07-18 (longest prefix wins).
    for name in sorted(DEFAULT_PRICES, key=len, reverse=True):
        if model == name or model.startswith(name + "-"):
            return DEFAULT_PRICES[name]
    return FALLBACK_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int, overrides: dict | None = None) -> float:
    pin, pout = price_for(model, overrides)
    return (input_tokens * pin + output_tokens * pout) / 1_000_000
