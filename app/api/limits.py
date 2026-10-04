"""Guards for the public demo: a per-client rate limit and a daily spending cap.

Both are answered from the database (the source of truth we already have), so they
survive restarts and work across several API instances without extra infrastructure.

Spend is attributed to the day a step FINISHED (steps record their cost when they end).
Money being spent right now isn't in the DB yet, so every in-flight run also reserves a
worst-case estimate; a new run starts only if `spent today + reserved + one more reserve`
stays under the cap. Costs are our estimates from token counts (`app/pricing.py`), not
provider invoices.

Known gap: the check and the insert aren't one transaction, so two requests arriving at
the same instant can both pass. Overshoot is bounded by one run's cost each; acceptable
for a low-traffic demo.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Run, RunStatus, Step, as_utc, utcnow

# A PENDING/RUNNING run untouched for this long is assumed lost (worker job_timeout is
# 10 min), so it stops holding a reserve and can't block the demo forever.
IN_FLIGHT_WINDOW = dt.timedelta(hours=1)
RATE_WINDOW = dt.timedelta(hours=1)


@dataclass(frozen=True)
class Limits:
    runs_per_client_per_hour: int | None = 3  # None = no rate limit
    daily_spend_usd: float | None = 2.00  # None = no cap
    reserve_per_run_usd: float = 0.50  # worst-case estimate for one run in flight


@dataclass(frozen=True)
class Refusal:
    status_code: int  # 429 = this client is too fast; 503 = demo out of budget today
    detail: str
    retry_after_s: int


def client_key(ip: str | None) -> str:
    """Pseudonymous key for rate limiting. We don't need raw IPs, so we don't store them.
    (A hash of an IP is pseudonymous, not anonymous: IPv4 space is small enough to
    brute-force. Good enough for rate limiting; don't treat it as anonymised data.)"""
    return hashlib.sha256((ip or "unknown").encode()).hexdigest()[:32]


def _start_of_day(now: dt.datetime) -> dt.datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def spend_today(s: AsyncSession, now: dt.datetime) -> tuple[float, int]:
    """(USD recorded by steps that finished today UTC, number of runs in flight)."""
    spent = await s.scalar(
        select(func.coalesce(func.sum(Step.cost_usd), 0.0)).where(
            Step.finished_at >= _start_of_day(now)
        )
    )
    in_flight = await s.scalar(
        select(func.count(Run.id)).where(
            Run.status.in_([RunStatus.PENDING, RunStatus.RUNNING]),
            Run.updated_at >= now - IN_FLIGHT_WINDOW,
        )
    )
    return float(spent or 0.0), int(in_flight or 0)


async def check_spend(s: AsyncSession, limits: Limits, now: dt.datetime) -> Refusal | None:
    if limits.daily_spend_usd is None:
        return None
    spent, in_flight = await spend_today(s, now)
    committed = spent + (in_flight + 1) * limits.reserve_per_run_usd
    if committed > limits.daily_spend_usd + 1e-9:
        tomorrow = _start_of_day(now) + dt.timedelta(days=1)
        return Refusal(503, "the demo has reached today's spending cap; try again tomorrow",
                       max(1, int((tomorrow - now).total_seconds())))  # fmt: skip
    return None


async def check_new_run(
    sessions: async_sessionmaker[AsyncSession],
    limits: Limits,
    key: str,
    now: dt.datetime | None = None,
) -> Refusal | None:
    """None = allowed. Rate limit first: it's per client and says nothing about budget."""
    now = now or utcnow()
    async with sessions() as s:
        if limits.runs_per_client_per_hour is not None:
            since = now - RATE_WINDOW
            recent = await s.scalars(
                select(Run.created_at)
                .where(Run.client_key == key, Run.created_at >= since)
                .order_by(Run.created_at)
            )
            times = list(recent)
            if len(times) >= limits.runs_per_client_per_hour:
                # Retry when the oldest run in the window ages out.
                wait = int((as_utc(times[0]) + RATE_WINDOW - now).total_seconds()) + 1
                return Refusal(429, f"rate limit: {limits.runs_per_client_per_hour} runs per "
                               "hour", max(1, wait))  # fmt: skip
        return await check_spend(s, limits, now)


async def check_resume(
    sessions: async_sessionmaker[AsyncSession], limits: Limits, now: dt.datetime | None = None
) -> Refusal | None:
    """Resuming costs money too, so it respects the spending cap."""
    async with sessions() as s:
        return await check_spend(s, limits, now or utcnow())
