"""API tests in-process (httpx ASGI transport), temp SQLite, fake queue. No Redis, no LLM."""

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from app.api.limits import Limits
from app.api.main import AppDeps, create_app
from app.db.models import Base, Run, RunStatus
from app.db.session import make_engine, make_sessionmaker
from app.pipeline.orchestrator import Orchestrator, StepContext, StepResult
from app.pipeline.steps import STEP_NAMES
from app.schemas import Sender
from tests.samples import SENDER

BODY = {"domain": "https://www.Acme.com/about", "person_name": "Ana Silva",
        "person_role": "Head of People"}  # fmt: skip


NO_LIMITS = Limits(runs_per_client_per_hour=None, daily_spend_usd=None)


class Env:
    def __init__(self, sessions, limits: Limits = NO_LIMITS, **deps) -> None:
        self.sessions = sessions
        self.enqueued: list[tuple[uuid.UUID, int]] = []
        # Anonymous allowed by default here (like the public demo); tests/test_auth.py
        # covers the locked-down default.
        deps.setdefault("allow_anonymous", True)
        self.deps = AppDeps(sessions, self.enqueue, SENDER, limits, poll_seconds=0.01, **deps)
        self.app = create_app(self.deps)
        self.client = self.client_from("127.0.0.1")

    def client_from(self, ip: str, key: str | None = None) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, client=(ip, 1234))
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return httpx.AsyncClient(transport=transport, base_url="http://t", headers=headers)

    async def enqueue(self, run_id: uuid.UUID, attempt: int) -> bool:
        self.enqueued.append((run_id, attempt))
        return True

    def orchestrator(self, fail_on: str | None = None) -> Orchestrator:
        def make(name: str):
            async def fn(ctx: StepContext) -> StepResult:
                if name == fail_on:
                    raise RuntimeError("boom")
                return StepResult({"ok": True}, cost_usd=0.01)

            return fn

        return Orchestrator(self.sessions, [(n, make(n)) for n in STEP_NAMES])

    async def set_status(self, run_id: str, status: RunStatus) -> None:
        async with self.sessions() as s:
            await s.execute(update(Run).where(Run.id == uuid.UUID(run_id)).values(status=status))
            await s.commit()


@pytest.fixture
async def env(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    e = Env(make_sessionmaker(engine))
    yield e
    await e.client.aclose()
    await engine.dispose()


async def test_health(env) -> None:
    assert (await env.client.get("/health")).json() == {"status": "ok"}


async def test_create_run_enqueues_and_normalises(env) -> None:
    r = await env.client.post("/runs", json=BODY)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert env.enqueued == [(uuid.UUID(run_id), 0)]

    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["status"] == "pending" and view["lead"]["domain"] == "acme.com"
    assert [s["name"] for s in view["steps"]] == STEP_NAMES
    assert view["email"] is None


PLACEHOLDER = Sender(name="Your Name", company="c", offer="o", outcomes=["x"])


@pytest.mark.parametrize("server_sender", [None, PLACEHOLDER])
async def test_create_refused_without_real_sender(env, server_sender) -> None:
    env.deps.sender = server_sender
    r = await env.client.post("/runs", json=BODY)
    assert r.status_code == 422 and env.enqueued == []


async def test_request_sender_overrides_server_default(env) -> None:
    env.deps.sender = None  # e.g. a multi-team deploy with no sender.json
    team = {"name": "Sam Lee", "company": "Shiftly", "offer": "Shift scheduling for clinics.",
            "outcomes": ["cut rota planning from 6h to 1h a week at Northside Clinic"]}  # fmt: skip
    r = await env.client.post("/runs", json={**BODY, "sender": team})
    assert r.status_code == 202
    async with env.sessions() as s:
        run = await s.get(Run, uuid.UUID(r.json()["run_id"]))
    assert run.sender["company"] == "Shiftly"  # snapshotted: the pipeline writes as this team


async def test_request_placeholder_sender_refused(env) -> None:
    r = await env.client.post("/runs", json={**BODY, "sender": PLACEHOLDER.model_dump()})
    assert r.status_code == 422 and env.enqueued == []


@pytest.mark.parametrize("patch", [{"outcomes": []}, {"outcomes": ["x"] * 6},
                                   {"offer": "o" * 401}, {"ask": "a" * 151},
                                   {"outcomes": ["x" * 201]}, {"surprise": 1}])  # fmt: skip
async def test_oversized_or_malformed_sender_is_422(env, patch) -> None:
    sender = {**SENDER.model_dump(), **patch}
    r = await env.client.post("/runs", json={**BODY, "sender": sender})
    assert r.status_code == 422 and env.enqueued == []


async def test_invalid_lead_is_422(env) -> None:
    r = await env.client.post("/runs", json=BODY | {"domain": "not a domain"})
    assert r.status_code == 422 and env.enqueued == []


async def test_unknown_run_is_404(env) -> None:
    for path in ("", "/events"):
        assert (await env.client.get(f"/runs/{uuid.uuid4()}{path}")).status_code == 404
    assert (await env.client.post(f"/runs/{uuid.uuid4()}/resume")).status_code == 404


async def test_view_after_execution(env) -> None:
    run_id = (await env.client.post("/runs", json=BODY)).json()["run_id"]
    await env.orchestrator().execute(uuid.UUID(run_id))
    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["status"] == "succeeded" and view["cost_usd"] == pytest.approx(0.06)
    assert all(s["status"] == "succeeded" for s in view["steps"])


async def test_resume_only_failed_runs(env) -> None:
    run_id = (await env.client.post("/runs", json=BODY)).json()["run_id"]
    assert (await env.client.post(f"/runs/{run_id}/resume")).status_code == 409  # pending

    await env.orchestrator(fail_on="research").execute(uuid.UUID(run_id))
    r = await env.client.post(f"/runs/{run_id}/resume")
    assert r.status_code == 202
    assert env.enqueued[-1] == (uuid.UUID(run_id), 1)  # new job id: not a duplicate
    # Shown as queued right away (found on real Redis: a re-opened progress stream saw
    # the stale "failed" and closed), and a second click can't queue it twice.
    assert (await env.client.get(f"/runs/{run_id}")).json()["status"] == "pending"
    assert (await env.client.post(f"/runs/{run_id}/resume")).status_code == 409
    assert len(env.enqueued) == 2


async def test_queue_outage_leaves_a_resumable_run(env) -> None:
    async def down(run_id: uuid.UUID, attempt: int) -> bool:
        raise ConnectionError("redis is down")

    env.deps.enqueue = down
    r = await env.client.post("/runs", json=BODY)
    assert r.status_code == 503
    run_id = r.json()["run_id"]
    view = (await env.client.get(f"/runs/{run_id}")).json()
    assert view["status"] == "failed" and "queue" in view["error"]

    env.deps.enqueue = env.enqueue  # queue is back
    assert (await env.client.post(f"/runs/{run_id}/resume")).status_code == 202
    assert env.enqueued == [(uuid.UUID(run_id), 0)]


async def read_events(client: httpx.AsyncClient, run_id: str) -> list[tuple[str, dict]]:
    events, name = [], None
    async with client.stream("GET", f"/runs/{run_id}/events") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[6:])))
    return events


async def test_sse_streams_until_terminal(env) -> None:
    run_id = (await env.client.post("/runs", json=BODY)).json()["run_id"]

    async def finish_later() -> None:
        await asyncio.sleep(0.05)
        await env.set_status(run_id, RunStatus.RUNNING)
        await asyncio.sleep(0.05)
        await env.set_status(run_id, RunStatus.NEEDS_HUMAN)

    task = asyncio.create_task(finish_later())
    events = await asyncio.wait_for(read_events(env.client, run_id), timeout=5)
    await task

    statuses = [data["status"] for name, data in events if name == "update"]
    assert statuses[0] == "pending" and statuses[-1] == "needs_human"
    assert "running" in statuses
    assert events[-1] == ("done", {"status": "needs_human"})
    assert len(statuses) == len(set(statuses))  # only changes are sent, not every poll
