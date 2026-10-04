"""Public-demo guards: per-client rate limit and daily spending cap. Temp SQLite, no LLM."""

import datetime as dt
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.api.limits import Limits, check_new_run, client_key
from app.db.models import Base, Run, RunStatus, Step, utcnow
from app.db.session import make_engine, make_sessionmaker
from tests.test_api import BODY, Env

RATE_ONLY = Limits(runs_per_client_per_hour=2, daily_spend_usd=None)
CAP_ONLY = Limits(runs_per_client_per_hour=None, daily_spend_usd=1.0, reserve_per_run_usd=0.25)


@pytest.fixture
async def sessions(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'l.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def finish_run(sessions, run_id: str, cost: float, finished_at: dt.datetime) -> None:
    """Simulate a worker finishing a run: one step records the spend, run leaves flight."""
    rid = uuid.UUID(run_id)
    async with sessions() as s:
        step = (await s.scalars(select(Step).where(Step.run_id == rid).limit(1))).one()
        step.cost_usd, step.finished_at = cost, finished_at
        await s.execute(update(Run).where(Run.id == rid).values(status=RunStatus.SUCCEEDED))
        await s.commit()


# --- rate limit ---


async def test_rate_limit_per_client(sessions) -> None:
    env = Env(sessions, RATE_ONLY)
    for _ in range(2):
        assert (await env.client.post("/runs", json=BODY)).status_code == 202
    r = await env.client.post("/runs", json=BODY)
    assert r.status_code == 429
    assert 0 < int(r.headers["Retry-After"]) <= 3601
    assert len(env.enqueued) == 2  # the refused request created and enqueued nothing

    other = env.client_from("10.0.0.9")  # a different client has its own allowance
    assert (await other.post("/runs", json=BODY)).status_code == 202
    await other.aclose()
    await env.client.aclose()


async def test_rate_limit_window_slides(sessions) -> None:
    key = client_key("1.2.3.4")
    env = Env(sessions, RATE_ONLY)
    for _ in range(2):
        await env.client_from("1.2.3.4").post("/runs", json=BODY)
    now = utcnow()
    assert (await check_new_run(sessions, RATE_ONLY, key, now)) is not None
    later = now + dt.timedelta(hours=1, seconds=1)
    assert (await check_new_run(sessions, RATE_ONLY, key, later)) is None
    await env.client.aclose()


async def test_client_ip_is_stored_hashed(sessions) -> None:
    env = Env(sessions, RATE_ONLY)
    await env.client_from("203.0.113.7").post("/runs", json=BODY)
    async with sessions() as s:
        stored = (await s.scalars(select(Run.client_key))).one()
    assert stored == client_key("203.0.113.7") and "203.0.113.7" not in stored
    await env.client.aclose()


# --- spending cap ---


async def test_in_flight_runs_reserve_budget(sessions) -> None:
    # cap 1.00, reserve 0.25: 4 queued runs reserve the whole budget, the 5th is refused.
    env = Env(sessions, CAP_ONLY)
    for _ in range(4):
        assert (await env.client.post("/runs", json=BODY)).status_code == 202
    r = await env.client.post("/runs", json=BODY)
    assert r.status_code == 503 and "spending cap" in r.json()["detail"]
    assert int(r.headers["Retry-After"]) <= 86_400
    await env.client.aclose()


async def test_recorded_spend_today_counts_and_yesterday_does_not(sessions) -> None:
    env = Env(sessions, CAP_ONLY)
    first = (await env.client.post("/runs", json=BODY)).json()["run_id"]
    await finish_run(sessions, first, cost=0.80, finished_at=utcnow())
    # spent 0.80 + one new reserve 0.25 > 1.00 cap
    assert (await env.client.post("/runs", json=BODY)).status_code == 503

    await finish_run(sessions, first, cost=0.80, finished_at=utcnow() - dt.timedelta(days=1))
    assert (await env.client.post("/runs", json=BODY)).status_code == 202
    await env.client.aclose()


async def test_lost_runs_stop_holding_a_reserve(sessions) -> None:
    env = Env(sessions, CAP_ONLY)
    for _ in range(4):
        await env.client.post("/runs", json=BODY)
    assert (await env.client.post("/runs", json=BODY)).status_code == 503
    async with sessions() as s:  # queued runs never picked up for 2 hours: assume lost
        await s.execute(update(Run).values(updated_at=utcnow() - dt.timedelta(hours=2)))
        await s.commit()
    assert (await env.client.post("/runs", json=BODY)).status_code == 202
    await env.client.aclose()


async def test_resume_respects_cap(sessions) -> None:
    env = Env(sessions, CAP_ONLY)
    run_id = (await env.client.post("/runs", json=BODY)).json()["run_id"]
    await env.set_status(run_id, RunStatus.FAILED)
    async with sessions() as s:  # the failed attempt already spent most of the budget
        step = (await s.scalars(select(Step).limit(1))).one()
        step.cost_usd, step.finished_at = 0.90, utcnow()
        await s.commit()
    r = await env.client.post(f"/runs/{run_id}/resume")
    assert r.status_code == 503 and len(env.enqueued) == 1  # only the original enqueue
    await env.client.aclose()


async def test_disabled_limits_allow_everything(sessions) -> None:
    off = Limits(runs_per_client_per_hour=None, daily_spend_usd=None)
    env = Env(sessions, off)
    for _ in range(10):
        assert (await env.client.post("/runs", json=BODY)).status_code == 202
    await env.client.aclose()
