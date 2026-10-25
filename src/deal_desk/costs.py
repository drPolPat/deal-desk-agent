from collections.abc import Mapping

# USD per million tokens: input, output, 5-minute cache write, cache read.
# Verified 2026-10-25 against platform.claude.com/docs/en/about-claude/pricing.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    # shortcut: the <=100K-token prompt tier; prompts here are ~6K tokens. Price the >100K tier
    # ($0.50/$2.50) if prompts ever grow that large.
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}


def cost_usd(model: str, usage: Mapping[str, int]) -> float | None:
    """None for a model without a known price, so an unpriced run is never reported as $0."""
    price = PRICES.get(model)
    if price is None:
        return None
    tokens = (usage.get("input_tokens", 0), usage.get("output_tokens", 0),
              usage.get("cache_creation_input_tokens", 0), usage.get("cache_read_input_tokens", 0))
    return sum(t * p for t, p in zip(tokens, price, strict=True)) / 1_000_000
