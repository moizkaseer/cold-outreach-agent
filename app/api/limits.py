"""Guards for the public demo: a per-client rate limit and a daily spending cap.

Both are answered from the database (the source of truth we already have), so they
survive restarts and work across several API instances without extra infrastructure.

Spend is attributed to the day a step FINISHED (steps record their cost when they end).
Money being spent right now isn't in the DB yet, so every in-flight run also reserves a
worst-case estimate; a new run starts only if `spent today + reserved + one more reserve`
stays under the cap. Costs are our estimates from token counts (`app/pricing.py`), not
provider invoices.

The check and the insert that follows must be one step, or a burst of simultaneous
requests all pass the check before any of them is recorded (measured on Postgres,
2026-10-10: 5 of 6 got through with one run left). `admission_lock` serialises them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Run, RunStatus, Step, as_utc, utcnow

# A PENDING/RUNNING run untouched for this long is assumed lost (worker job_timeout is
# 10 min), so it stops holding a reserve and can't block the demo forever.
IN_FLIGHT_WINDOW = dt.timedelta(hours=1)
RATE_WINDOW = dt.timedelta(hours=1)


ADMISSION_LOCK_KEY = 7_203_001  # arbitrary; just has to be the same in every API process
_local_lock = asyncio.Lock()  # SQLite (dev, tests): one process, so an in-process lock does


@asynccontextmanager
async def admission_lock(sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    """Hold this around "check the limits, then record the run".

    On Postgres it takes a transaction-level advisory lock: a named lock in the database
    itself, so it also works across several API instances, and it is released
    automatically when the transaction ends, even if the request crashes. Everyone else
    waits their turn and then sees the run that was just recorded. The guarded section is
    a few fast queries; a waiter holds one pooled connection while it waits, which is
    fine at demo traffic and would be the thing to revisit at high volume.
    """
    async with sessions() as s:
        if s.get_bind().dialect.name != "postgresql":
            async with _local_lock:
                yield
            return
        async with s.begin():
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADMISSION_LOCK_KEY})
            yield


@dataclass(frozen=True)
class Limits:
    runs_per_client_per_hour: int | None = 3  # anonymous, per IP. None = no rate limit
    runs_per_key_per_hour: int | None = 60  # per API key
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
    *,
    keyed: bool = False,
) -> Refusal | None:
    """None = allowed. Rate limit first: it's per client and says nothing about budget.
    `keyed` = the client is an API key (its own, higher allowance), not an anonymous IP."""
    now = now or utcnow()
    per_hour = limits.runs_per_key_per_hour if keyed else limits.runs_per_client_per_hour
    async with sessions() as s:
        if per_hour is not None:
            since = now - RATE_WINDOW
            recent = await s.scalars(
                select(Run.created_at)
                .where(Run.client_key == key, Run.created_at >= since)
                .order_by(Run.created_at)
            )
            times = list(recent)
            if len(times) >= per_hour:
                # Retry when the oldest run in the window ages out.
                wait = int((as_utc(times[0]) + RATE_WINDOW - now).total_seconds()) + 1
                return Refusal(429, f"rate limit: {per_hour} runs per hour", max(1, wait))
        return await check_spend(s, limits, now)


async def check_resume(
    sessions: async_sessionmaker[AsyncSession], limits: Limits, now: dt.datetime | None = None
) -> Refusal | None:
    """Resuming costs money too, so it respects the spending cap."""
    async with sessions() as s:
        return await check_spend(s, limits, now or utcnow())
