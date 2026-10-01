"""Token usage accounting (its own module so pricing and the loop don't import each other)."""

from dataclasses import dataclass
from typing import Any


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total(self) -> int:
        # Every turn re-sends the whole history, so input tokens grow each turn. That's
        # real spend, so it counts towards the budget.
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def add(self, u: Any) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read_input_tokens += getattr(u, "cache_read_input_tokens", None) or 0
        self.cache_creation_input_tokens += getattr(u, "cache_creation_input_tokens", None) or 0
