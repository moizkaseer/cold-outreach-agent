"""Prove the backend plumbing on REAL infrastructure, for free.

    docker compose up -d postgres redis
    uv run python -m scripts.infra_smoke

Starts the real API (uvicorn) and a real Arq worker as separate processes against the
local Postgres + Redis, then drives them over HTTP like a client would:

    migrations -> POST /runs -> Redis -> worker -> Postgres -> SSE progress -> email
    a failing step -> POST /runs/{id}/resume -> finishes from the failed step
    duplicate enqueue is ignored, the per-client rate limit answers 429

Only the pipeline STEPS are fake (they sleep and write canned output), so no LLM or
search money is spent. Everything around them is the production code path. The sender
and the email this writes are test data; nothing here can be sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from typing import Any

import httpx
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select, text

from app.api.auth import create_api_key
from app.db.models import EmailDraft, EmailStatus
from app.db.session import make_engine, make_sessionmaker
from app.pipeline.orchestrator import Orchestrator, StepContext, StepFn, StepResult
from app.pipeline.steps import STEP_NAMES
from app.workers.queue import enqueue_run
from app.workers.worker import run_pipeline

# Its own throwaway database and Redis db, recreated on every run: reruns start clean
# (the rate-limit check below depends on that) and dev data is never touched.
ADMIN_URL = "postgresql+asyncpg://postgres:outreach@localhost:5432/postgres"
DATABASE_URL = "postgresql+asyncpg://postgres:outreach@localhost:5432/outreach_smoke"
REDIS_URL = "redis://localhost:6379/1"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
FAIL_ONCE_DOMAIN = "failonce-smoke.com"

SENDER = {
    "name": "Sam Test",
    "company": "Onboardly",
    "offer": "We help B2B software companies fix the onboarding steps where users drop off.",
    "outcomes": ["cut time-to-first-value from 9 days to 3 for a payroll software company"],
}


# --- the worker side: real Arq worker, fake steps ---


def fake_steps(sessions: Any) -> list[tuple[str, StepFn]]:
    failed_once: set[uuid.UUID] = set()

    def make(name: str) -> StepFn:
        async def step(ctx: StepContext) -> StepResult:
            await asyncio.sleep(0.4)  # long enough for SSE to observe each step
            if (name == "research" and ctx.lead.domain == FAIL_ONCE_DOMAIN
                    and ctx.run_id not in failed_once):  # fmt: skip
                failed_once.add(ctx.run_id)
                raise RuntimeError("simulated outage")
            if name == "draft":
                async with sessions() as s:
                    row = (await s.execute(select(EmailDraft).where(
                        EmailDraft.run_id == ctx.run_id))).scalar_one_or_none()  # fmt: skip
                    row = row or EmailDraft(run_id=ctx.run_id)
                    row.subject, row.body = "smoke test", f"Hi {ctx.lead.person_name}, test only."
                    row.used_fact_ids, row.status = ["f1"], EmailStatus.VERIFIED
                    row.problems = []
                    s.add(row)
                    await s.commit()
            return StepResult({"fake": name}, cost_usd=0.01)

        return step

    return [(n, make(n)) for n in STEP_NAMES]


async def _startup(ctx: dict[str, Any]) -> None:
    engine = make_engine(DATABASE_URL)
    sessions = make_sessionmaker(engine)
    ctx["engine"] = engine
    ctx["orchestrator"] = Orchestrator(sessions, fake_steps(sessions))


async def _shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


class SmokeWorkerSettings:
    functions = [run_pipeline]  # the production job function
    on_startup = _startup
    on_shutdown = _shutdown
    redis_settings = RedisSettings.from_dsn(REDIS_URL)
    max_tries = 1


# --- the client side ---


class Check:
    def __init__(self) -> None:
        self.failed = 0

    def __call__(self, ok: bool, what: str, detail: str = "") -> None:
        self.failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {what}" + (f"  ({detail})" if detail else ""))


async def follow(client: httpx.AsyncClient, run_id: str, headers: dict[str, str] | None = None
                 ) -> tuple[list[dict], str | None]:  # fmt: skip
    """Read the SSE stream to its end: (every update payload, final status)."""
    updates: list[dict] = []
    name = final = None
    async with client.stream("GET", f"/runs/{run_id}/events", timeout=60,
                             headers=headers) as r:  # fmt: skip
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
                if name == "update":
                    updates.append(data)
                elif name == "done":
                    final = data["status"]
    return updates, final


async def wait_for_api(client: httpx.AsyncClient) -> None:
    for _ in range(100):
        try:
            if (await client.get("/health")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("API did not start")


async def drive(check: Check) -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        await wait_for_api(c)
        lead = {"person_name": "Ana Silva", "person_role": "Head of People", "sender": SENDER}

        print("happy path")
        r = await c.post("/runs", json={**lead, "domain": f"smoke-{uuid.uuid4().hex[:8]}.com"})
        check(r.status_code == 202, "POST /runs accepted", str(r.status_code))
        run_id = r.json()["run_id"]
        updates, final = await follow(c, run_id)
        seen = [u["current_step"] for u in updates if u["current_step"]]
        check(final == "succeeded", "worker finished the run", str(final))
        # The stream polls once a second and fake steps take 0.4s, so it catches 2-3 of 6.
        check(len(set(seen)) >= 2, "SSE showed steps progressing", f"saw {sorted(set(seen))}")
        view = (await c.get(f"/runs/{run_id}")).json()
        check(all(s["status"] == "succeeded" for s in view["steps"]), "all 6 steps saved")
        check(abs(view["cost_usd"] - 0.06) < 1e-6, "cost summed in Postgres", str(view["cost_usd"]))
        check(view["email"] is not None and view["email"]["status"] == "verified", "email saved")

        print("fail, then resume")
        r = await c.post("/runs", json={**lead, "domain": FAIL_ONCE_DOMAIN})
        run_id = r.json()["run_id"]
        _, final = await follow(c, run_id)
        view = (await c.get(f"/runs/{run_id}")).json()
        check(final == "failed" and view["current_step"] == "research", "run failed at research",
              f"{final} at {view['current_step']}")  # fmt: skip
        r = await c.post(f"/runs/{run_id}/resume")
        check(r.status_code == 202, "resume accepted", str(r.status_code))
        _, final = await follow(c, run_id)
        view = (await c.get(f"/runs/{run_id}")).json()
        attempts = {s["name"]: s["attempts"] for s in view["steps"]}
        check(final == "succeeded", "resumed run finished", str(final))
        check(attempts["research"] == 2 and attempts["score"] == 1,
              "only the failed step re-ran", str(attempts))  # fmt: skip
        check(abs(view["cost_usd"] - 0.06) < 1e-6, "nothing paid twice", str(view["cost_usd"]))

        print("API keys")
        engine = make_engine(DATABASE_URL)
        (_, acme), (_, rival) = [await create_api_key(make_sessionmaker(engine), n)
                                 for n in ("Acme", "Rival")]  # fmt: skip
        await engine.dispose()
        as_acme, as_rival = ({"Authorization": f"Bearer {k}"} for k in (acme, rival))
        r = await c.post("/runs", headers=as_acme,
                         json={**lead, "domain": f"keyed-{uuid.uuid4().hex[:8]}.com"})  # fmt: skip
        run_id = r.json()["run_id"]
        _, final = await follow(c, run_id, as_acme)
        check(final == "succeeded", "run created with a key finished", str(final))
        codes = [(await c.get(f"/runs/{run_id}", headers=h)).status_code
                 for h in (as_acme, as_rival, {})]  # fmt: skip
        check(codes == [200, 404, 404], "only the owning key can read it", str(codes))
        mine = (await c.get("/runs", headers=as_acme)).json()
        theirs = (await c.get("/runs", headers=as_rival)).json()
        check([m["run_id"] for m in mine] == [run_id] and theirs == [], "GET /runs is per team")
        r = await c.get("/runs", headers={"Authorization": "Bearer oa_wrong"})
        check(r.status_code == 401, "wrong key is 401", str(r.status_code))

        print("guards")
        redis = await create_pool(RedisSettings.from_dsn(REDIS_URL))
        rid = uuid.uuid4()  # never created: the worker will log an error for it, harmlessly
        first, second = await enqueue_run(redis, rid, 9), await enqueue_run(redis, rid, 9)
        await redis.aclose()
        check(first and not second, "duplicate enqueue ignored by Redis")
        codes = [(await c.post("/runs", json={**lead, "domain": f"limit{i}-smoke.com"})).status_code
                 for i in range(2)]  # fmt: skip
        check(codes == [202, 429], "4th run in an hour from one client gets 429", str(codes))


async def reset() -> None:
    engine = make_engine(ADMIN_URL).execution_options(isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:  # CREATE DATABASE can't run in a transaction
        await conn.execute(text("DROP DATABASE IF EXISTS outreach_smoke WITH (FORCE)"))
        await conn.execute(text("CREATE DATABASE outreach_smoke"))
    await engine.dispose()
    await reset_queue()


async def reset_queue() -> None:
    redis = await create_pool(RedisSettings.from_dsn(REDIS_URL))
    await redis.flushdb()
    await redis.aclose()


def main() -> int:
    env = {**os.environ, "DATABASE_URL": DATABASE_URL, "REDIS_URL": REDIS_URL,
           "ALLOW_ANONYMOUS_RUNS": "true"}  # the anonymous path is exercised too  # fmt: skip
    asyncio.run(reset())
    print("migrations")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env, check=True,
                   capture_output=True)  # fmt: skip
    print("  PASS  alembic upgrade head on Postgres")

    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    api = [sys.executable, "-m", "uvicorn", "app.api.main:app", "--port", str(PORT)]
    worker = [sys.executable, "-m", "arq", "scripts.infra_smoke.SmokeWorkerSettings"]
    procs = [
        subprocess.Popen([*api, "--log-level", "warning"], env=env),
        subprocess.Popen(worker, env=env, **quiet),
    ]
    check = Check()
    try:
        asyncio.run(drive(check))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=10)
        # Leave nothing queued. A leftover job is not harmless: the next REAL worker to
        # start on this Redis would run it with real agents and spend money (this
        # happened once, 2026-10-10, before this script had its own Redis db).
        asyncio.run(reset_queue())
    print("FAILED" if check.failed else "ALL PASSED")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
