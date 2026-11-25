"""USD per 1M tokens (input, output). VERIFY against your provider's current price list;
override per model on /admin/settings ("LLM price overrides")."""
from __future__ import annotations

DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
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
