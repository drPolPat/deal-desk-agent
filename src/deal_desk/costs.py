from collections.abc import Mapping

# USD per million tokens: input, output, 5-minute cache write, cache read.
# shortcut: copied from the bundled Claude API reference (cached 2026-10-06); re-check against
# anthropic.com/pricing before publishing numbers (M3 model comparison).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),  # prompts up to 100K tokens
}


def cost_usd(model: str, usage: Mapping[str, int]) -> float | None:
    """None for a model without a known price, so an unpriced run is never reported as $0."""
    price = PRICES.get(model)
    if price is None:
        return None
    tokens = (usage.get("input_tokens", 0), usage.get("output_tokens", 0),
              usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0))
    return sum(t * p for t, p in zip(tokens, price, strict=True)) / 1_000_000
