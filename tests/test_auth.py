"""API keys, ownership and CORS. In-process, temp SQLite, fake queue."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update

from app.api.auth import create_api_key, hash_key
from app.api.limits import Limits
from app.db.models import ApiKey, Base, Run, utcnow
from app.db.session import make_engine, make_sessionmaker
from tests.test_api import BODY, NO_LIMITS, Env


@pytest.fixture
async def sessions(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def team(env: Env, name: str):
    row, raw = await create_api_key(env.sessions, name)
    return row, env.client_from("127.0.0.1", raw)


async def test_only_the_hash_is_stored(sessions) -> None:
    row, raw = await create_api_key(sessions, "Acme sales")
    assert raw.startswith("oa_") and len(raw) > 40
    async with sessions() as s:
        stored = (await s.execute(select(ApiKey))).scalar_one()
    assert stored.key_hash == hash_key(raw) and raw not in repr(vars(stored))
    assert stored.prefix == raw[:10]


async def test_locked_down_by_default(sessions) -> None:
    env = Env(sessions, allow_anonymous=False)
    r = await env.client.post("/runs", json=BODY)
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    assert env.enqueued == []
    assert (await env.client.get("/runs")).status_code == 401
    assert (await env.client.get("/health")).status_code == 200  # health stays open


@pytest.mark.parametrize("header", ["Bearer oa_wrong", "Bearer ", "Basic abc", "oa_nope"])
async def test_bad_credentials_are_401_even_where_anonymous_is_allowed(sessions, header) -> None:
    # A wrong key must not quietly become an anonymous run nobody owns.
    env = Env(sessions, allow_anonymous=True)
    r = await env.client.post("/runs", json=BODY, headers={"Authorization": header})
    assert r.status_code == 401 and env.enqueued == []


async def test_revoked_key_stops_working(sessions) -> None:
    env = Env(sessions, allow_anonymous=False)
    row, client = await team(env, "Acme")
    assert (await client.post("/runs", json=BODY)).status_code == 202
    async with sessions() as s:
        await s.execute(update(ApiKey).where(ApiKey.id == row.id).values(revoked_at=utcnow()))
        await s.commit()
    assert (await client.post("/runs", json=BODY)).status_code == 401


async def test_a_team_sees_only_its_own_runs(sessions) -> None:
    env = Env(sessions, allow_anonymous=True)
    acme_row, acme = await team(env, "Acme")
    _, rival = await team(env, "Rival")

    run_id = (await acme.post("/runs", json=BODY)).json()["run_id"]
    async with sessions() as s:
        assert (await s.get(Run, uuid.UUID(run_id))).api_key_id == acme_row.id

    assert (await acme.get(f"/runs/{run_id}")).status_code == 200
    # Another team and anonymous callers get 404, same as a run that doesn't exist.
    for other in (rival, env.client):
        assert (await other.get(f"/runs/{run_id}")).status_code == 404
        assert (await other.get(f"/runs/{run_id}/events")).status_code == 404
        assert (await other.post(f"/runs/{run_id}/resume")).status_code == 404

    await rival.post("/runs", json=BODY)
    listed = (await acme.get("/runs")).json()
    assert [r["run_id"] for r in listed] == [run_id]
    assert listed[0]["lead"]["domain"] == "acme.com" and listed[0]["email_status"] is None


async def test_anonymous_run_is_readable_by_its_id(sessions) -> None:
    env = Env(sessions, allow_anonymous=True)
    run_id = (await env.client.post("/runs", json=BODY)).json()["run_id"]
    _, acme = await team(env, "Acme")
    assert (await env.client.get(f"/runs/{run_id}")).status_code == 200
    assert (await acme.get(f"/runs/{run_id}")).status_code == 200  # the id is the link
    assert (await acme.get("/runs")).json() == []  # ...but it isn't in any team's list


async def test_list_runs_pages_newest_first(sessions) -> None:
    env = Env(sessions)
    _, acme = await team(env, "Acme")
    ids = [(await acme.post("/runs", json=BODY)).json()["run_id"] for _ in range(3)]
    page1 = (await acme.get("/runs", params={"limit": 2})).json()
    assert [r["run_id"] for r in page1] == [ids[2], ids[1]]
    page2 = (await acme.get("/runs", params={"limit": 2, "before": page1[-1]["created_at"]})).json()
    assert [r["run_id"] for r in page2] == [ids[0]]
    assert (await acme.get("/runs", params={"limit": 101})).status_code == 422


async def test_keys_are_rate_limited_per_key_not_per_ip(sessions) -> None:
    limits = Limits(runs_per_client_per_hour=1, runs_per_key_per_hour=2, daily_spend_usd=None)
    env = Env(sessions, limits)
    row, raw = await create_api_key(sessions, "Acme")
    for ip in ("10.0.0.1", "10.0.0.2"):  # same key from two machines shares one allowance
        assert (await env.client_from(ip, raw).post("/runs", json=BODY)).status_code == 202
    assert (await env.client_from("10.0.0.3", raw).post("/runs", json=BODY)).status_code == 429
    # Anonymous callers on the same IP keep their own (smaller) allowance.
    assert (await env.client_from("10.0.0.1").post("/runs", json=BODY)).status_code == 202
    assert (await env.client_from("10.0.0.1").post("/runs", json=BODY)).status_code == 429


async def test_cors_only_for_configured_origins(sessions) -> None:
    env = Env(sessions, NO_LIMITS, cors_origins=("https://moizali.vercel.app",))
    pre = {"Access-Control-Request-Method": "POST",
           "Access-Control-Request-Headers": "authorization,content-type"}  # fmt: skip
    ok = await env.client.options("/runs", headers={**pre, "Origin": "https://moizali.vercel.app"})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == "https://moizali.vercel.app"
    assert "access-control-allow-credentials" not in ok.headers
    bad = await env.client.options("/runs", headers={**pre, "Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


async def test_no_cors_headers_unless_configured(sessions) -> None:
    env = Env(sessions)
    r = await env.client.get("/health", headers={"Origin": "https://moizali.vercel.app"})
    assert "access-control-allow-origin" not in r.headers
