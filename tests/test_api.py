"""API tests in-process (httpx ASGI transport), temp SQLite, fake queue. No Redis, no LLM."""

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import update

from app.api.main import AppDeps, create_app
from app.db.models import Base, Run, RunStatus
from app.db.session import make_engine, make_sessionmaker
from app.pipeline.orchestrator import Orchestrator, StepContext, StepResult
from app.pipeline.steps import STEP_NAMES
from app.schemas import Sender
from tests.samples import SENDER

BODY = {"domain": "https://www.Acme.com/about", "person_name": "Ana Silva",
        "person_role": "Head of People"}  # fmt: skip


class Env:
    def __init__(self, sessions) -> None:
        self.sessions = sessions
        self.enqueued: list[tuple[uuid.UUID, int]] = []
        self.deps = AppDeps(sessions, self.enqueue, SENDER, poll_seconds=0.01)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.deps)), base_url="http://t"
        )

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


@pytest.mark.parametrize("sender", [None, Sender(name="Your Name", company="c", offer="o",
                                                  outcomes=["x"])])  # fmt: skip
async def test_create_refused_without_real_sender(env, sender) -> None:
    env.deps.sender = sender
    r = await env.client.post("/runs", json=BODY)
    assert r.status_code == 503 and env.enqueued == []


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
