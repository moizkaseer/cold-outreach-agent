"""Estimated LLM cost from token usage.

Prices are USD per million tokens, from Anthropic's published pricing as of 2026-09-25.
Cache reads bill at 0.1x the input price and 5-minute cache writes at 1.25x. Update this
table when prices change; unknown models return None rather than a guessed number.
"""

from app.agents.loop import Usage

# model id -> (input $/MTok, output $/MTok)
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-opus-5-5": (4.00, 20.00),
}

CACHE_READ_MULT = 0.1
CACHE_WRITE_MULT = 1.25


def estimate_cost_usd(model: str, usage: Usage) -> float | None:
    if model not in PRICES:
        return None
    p_in, p_out = PRICES[model]
    total = (
        usage.input_tokens * p_in
        + usage.cache_read_input_tokens * p_in * CACHE_READ_MULT
        + usage.cache_creation_input_tokens * p_in * CACHE_WRITE_MULT
        + usage.output_tokens * p_out
    )
    return total / 1_000_000
