"""Spending cap for anything that calls paid APIs.

Used by evals now (`--max-cost`), and later by the public demo's daily cap. The cap stops
NEW work from starting once spend reaches the limit; work already in flight finishes, so
real spend can overshoot by at most the cost of the in-flight runs (each one is itself
bounded by its own token budget).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpendTracker:
    limit_usd: float | None  # None = no cap
    spent_usd: float = 0.0

    def add(self, usd: float | None) -> None:
        if usd:
            self.spent_usd += usd

    @property
    def exhausted(self) -> bool:
        # Tolerance for float sums: 0.04 + 0.01 + 0.04 + 0.01 == 0.09999999999999999,
        # which must still count as hitting a $0.10 cap.
        return self.limit_usd is not None and self.spent_usd >= self.limit_usd - 1e-9

    @property
    def remaining_usd(self) -> float | None:
        return None if self.limit_usd is None else max(0.0, self.limit_usd - self.spent_usd)
