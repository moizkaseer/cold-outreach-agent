"""HTTP API. Runs are created here and executed by the worker, never in the request.

    uv run uvicorn app.api.main:app --reload

    POST /runs                 create + enqueue a run         -> 202 {run_id}
                               (body carries the caller's sender profile)
    GET  /runs                 this team's runs, newest first (API key required)
    GET  /runs/{id}            status, steps, email, cost
    GET  /runs/{id}/events     live progress (Server-Sent Events) until the run ends
    POST /runs/{id}/resume     re-enqueue a FAILED run (resumes at the failed step)
    GET  /health
    /emails...                 human review: queue, evidence, edit, approve, reject
                               (app/api/review.py)

Who may do what is in app/api/deps.py (API keys; anonymous only if the deploy allows it).
Guards (app/api/limits.py): new runs are rate-limited per client (429) and everything
that spends money respects a daily spending cap (503).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import logfire
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.api import review
from app.api.deps import AppDeps, Caller, Team, get_deps
from app.api.limits import Limits, Refusal, check_new_run, check_resume, client_key
from app.db.models import Run, RunStatus, utcnow
from app.pipeline.orchestrator import create_run
from app.pipeline.steps import STEP_NAMES
from app.schemas import Lead, Sender, load_sender

TERMINAL = {RunStatus.SUCCEEDED, RunStatus.NEEDS_HUMAN, RunStatus.STOPPED, RunStatus.FAILED}
SSE_MAX_SECONDS = 15 * 60
SSE_HEARTBEAT_SECONDS = 15.0  # keeps proxies from closing an idle stream


PageLimit = Annotated[int, Query(ge=1, le=100)]
Before = Annotated[dt.datetime | None, Query(description="created_at of the last row seen")]


def refused(r: Refusal) -> JSONResponse:
    return JSONResponse({"detail": r.detail}, status_code=r.status_code,
                        headers={"Retry-After": str(r.retry_after_s)})  # fmt: skip


async def enqueue_or_fail(d: AppDeps, run_id: uuid.UUID, attempt: int) -> JSONResponse | None:
    """Queue the run. If the queue is down, don't leave the run PENDING forever with no
    job behind it: mark it FAILED (so it can be resumed) and tell the client."""
    try:
        await d.enqueue(run_id, attempt)
    except Exception:
        logfire.exception("enqueue failed", run_id=str(run_id))
        async with d.sessions() as s:
            await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == RunStatus.PENDING)
                .values(status=RunStatus.FAILED, error="could not queue the run; resume to retry")
            )
            await s.commit()
        return JSONResponse({"detail": "job queue unavailable; the run is saved, resume it later",
                             "run_id": str(run_id)}, status_code=503)  # fmt: skip
    return None


# --- request / response models ---


class CreateRun(BaseModel):
    domain: str
    person_name: str
    person_role: str
    # Each sales team writes as itself: who they are, what they sell, results they can
    # prove. Optional only so a single-tenant deploy can rely on sender.json instead.
    # Safe to accept from strangers: it reaches only this run's writer + verifier prompts
    # (length-capped in the schema), and no agent has tools that act outside the run.
    sender: Sender | None = None


class StepView(BaseModel):
    name: str
    status: str
    attempts: int
    error: str | None
    cost_usd: float


class EmailView(BaseModel):
    email_id: uuid.UUID  # for the review routes (/emails/{id})
    subject: str
    body: str
    status: str
    problems: list[str]


class RunView(BaseModel):
    run_id: uuid.UUID
    status: str
    current_step: str | None
    error: str | None
    cost_usd: float
    lead: CreateRun
    steps: list[StepView]
    email: EmailView | None
    created_at: dt.datetime


class RunSummary(BaseModel):
    run_id: uuid.UUID
    status: str
    lead: CreateRun
    email_status: str | None
    cost_usd: float
    created_at: dt.datetime


def _viewer_id(caller: Caller) -> uuid.UUID | None:
    return caller.id if caller else None


async def load_run_view(sessions: async_sessionmaker[AsyncSession], run_id: uuid.UUID,
                        viewer: uuid.UUID | None) -> RunView | None:  # fmt: skip
    """None if the run doesn't exist OR belongs to another team (callers answer 404 for
    both, so ids can't be probed). Anonymous runs are visible to anyone with the id."""
    async with sessions() as s:
        run = (
            await s.execute(
                select(Run)
                .where(Run.id == run_id)
                .options(selectinload(Run.steps), selectinload(Run.lead), selectinload(Run.email))
            )
        ).scalar_one_or_none()
        if run is None or (run.api_key_id is not None and run.api_key_id != viewer):
            return None
        return RunView(
            run_id=run.id,
            status=run.status.value,
            current_step=run.current_step,
            error=run.error,
            cost_usd=round(run.cost_usd, 4),
            lead=CreateRun(
                domain=run.lead.domain,
                person_name=run.lead.person_name,
                person_role=run.lead.person_role,
            ),  # fmt: skip
            steps=[
                StepView(
                    name=st.name,
                    status=st.status.value,
                    attempts=st.attempts,
                    error=st.error,
                    cost_usd=round(st.cost_usd, 4),
                )
                for st in run.steps
            ],  # fmt: skip
            email=EmailView(
                email_id=run.email.id,
                subject=run.email.subject,
                body=run.email.body,
                status=run.email.status.value,
                problems=list(run.email.problems),
            )
            if run.email
            else None,  # fmt: skip
            created_at=run.created_at,
        )


# --- app ---


def _load_sender_if_present(path: Path) -> Sender | None:
    return load_sender(path) if path.exists() else None


def create_app(deps: AppDeps | None = None) -> FastAPI:
    """Pass `deps` in tests; without it, real ones are built at startup from settings."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            app.state.deps = deps
            yield
            return
        from arq import create_pool
        from arq.connections import RedisSettings

        from app.config import get_settings
        from app.db.session import make_engine, make_sessionmaker
        from app.observability import setup_tracing
        from app.workers.queue import enqueue_run

        s = get_settings()
        setup_tracing("outreach-api", s.logfire_token)
        logfire.instrument_fastapi(app)  # request spans; agent spans come from the worker
        engine = make_engine(s.database_url)
        redis = await create_pool(RedisSettings.from_dsn(s.redis_url))
        sender = await asyncio.to_thread(_load_sender_if_present, Path("sender.json"))

        async def enqueue(run_id: uuid.UUID, attempt: int) -> bool:
            return await enqueue_run(redis, run_id, attempt)

        limits = Limits(
            runs_per_client_per_hour=s.demo_runs_per_client_per_hour or None,
            runs_per_key_per_hour=s.key_runs_per_hour or None,
            daily_spend_usd=s.demo_daily_spend_usd or None,
            reserve_per_run_usd=s.demo_reserve_per_run_usd,
        )
        app.state.deps = AppDeps(make_sessionmaker(engine), enqueue, sender, limits,
                                 allow_anonymous=s.allow_anonymous_runs)  # fmt: skip
        yield
        await redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Outreach Agent", lifespan=lifespan)
    if deps is not None:
        app.state.deps = deps  # available even when the server lifespan isn't run (tests)

    # Middleware must be added before startup, so origins are read here, not in lifespan.
    # InfraSettings needs no API keys, so importing this module stays safe anywhere.
    if deps is not None:
        origins = deps.cors_origins
    else:
        from app.config import InfraSettings

        origins = InfraSettings().cors_origin_list
    if origins:
        # No cookies are used (auth is a header), so credentials stay off; that keeps a
        # logged-in browser from being used against the API by another site.
        app.add_middleware(CORSMiddleware, allow_origins=list(origins),
                           allow_methods=["GET", "POST", "PATCH"],
                           allow_headers=["Authorization", "Content-Type"])  # fmt: skip

    app.include_router(review.router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", status_code=202, response_model=None)
    async def post_run(body: CreateRun, request: Request, caller: Caller
                       ) -> dict[str, str] | JSONResponse:  # fmt: skip
        d = get_deps(request)
        if caller is None and not d.allow_anonymous:
            raise HTTPException(401, "API key required",
                                headers={"WWW-Authenticate": "Bearer"})  # fmt: skip
        sender = body.sender or d.sender
        if sender is None:
            raise HTTPException(422, "sender profile required (who you are, offer, outcomes)")
        if sender.is_placeholder:
            raise HTTPException(422, "sender profile is the example placeholder; use real details")
        try:
            lead = Lead(domain=body.domain, person_name=body.person_name,
                        person_role=body.person_role)  # fmt: skip
        except ValidationError as e:
            # include_context=False: the context holds exception objects that can't be
            # JSON-encoded (that turned a clean 422 into a 500 before this fix).
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
        # request.client is the direct peer; behind the host's proxy uvicorn must run with
        # --proxy-headers so this is the real client IP, not the proxy's.
        # A team is limited per key (wherever it calls from); anonymous callers per IP.
        if caller is not None:
            key = caller.id.hex
        else:
            key = client_key(request.client.host if request.client else None)
        if refusal := await check_new_run(d.sessions, d.limits, key, keyed=caller is not None):
            return refused(refusal)
        run_id = await create_run(d.sessions, lead, sender, STEP_NAMES, client_key=key,
                                  api_key_id=_viewer_id(caller))  # fmt: skip
        if failure := await enqueue_or_fail(d, run_id, 0):
            return failure
        return {"run_id": str(run_id), "status": RunStatus.PENDING.value}

    @app.get("/runs")
    async def list_runs(
        request: Request,
        team: Team,
        limit: PageLimit = 20,
        before: Before = None,
    ) -> list[RunSummary]:
        q = (
            select(Run)
            .where(Run.api_key_id == team.id)
            .options(selectinload(Run.lead), selectinload(Run.email))
            .order_by(Run.created_at.desc())
            .limit(limit)
        )
        if before is not None:
            q = q.where(Run.created_at < before)
        async with get_deps(request).sessions() as s:
            runs = (await s.execute(q)).scalars().all()
        return [
            RunSummary(
                run_id=r.id,
                status=r.status.value,
                lead=CreateRun(domain=r.lead.domain, person_name=r.lead.person_name,
                               person_role=r.lead.person_role),
                email_status=r.email.status.value if r.email else None,
                cost_usd=round(r.cost_usd, 4),
                created_at=r.created_at,
            )
            for r in runs
        ]  # fmt: skip

    @app.get("/runs/{run_id}")
    async def get_run(run_id: uuid.UUID, request: Request, caller: Caller) -> RunView:
        view = await load_run_view(get_deps(request).sessions, run_id, _viewer_id(caller))
        if view is None:
            raise HTTPException(404, "run not found")
        return view

    @app.post("/runs/{run_id}/resume", status_code=202, response_model=None)
    async def resume_run(run_id: uuid.UUID, request: Request, caller: Caller
                         ) -> dict[str, str] | JSONResponse:  # fmt: skip
        d = get_deps(request)
        view = await load_run_view(d.sessions, run_id, _viewer_id(caller))
        if view is None:
            raise HTTPException(404, "run not found")
        if view.status != RunStatus.FAILED.value:
            raise HTTPException(409, f"only failed runs can be resumed (status: {view.status})")
        if refusal := await check_resume(d.sessions, d.limits):
            return refused(refusal)
        # FAILED -> PENDING before queueing, in one conditional UPDATE. Without it the run
        # still reads "failed" until a worker picks it up, so a client re-opening the
        # progress stream is told it's over. It also makes a double-clicked resume a 409.
        async with d.sessions() as s:
            claimed = await s.execute(
                update(Run)
                .where(Run.id == run_id, Run.status == RunStatus.FAILED)
                .values(status=RunStatus.PENDING, error=None, updated_at=utcnow())
            )
            await s.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "run is already being resumed")
        # New job id per resume attempt, so it isn't swallowed as a duplicate enqueue.
        attempt = max((st.attempts for st in view.steps), default=0)
        if failure := await enqueue_or_fail(d, run_id, attempt):
            return failure
        return {"run_id": str(run_id), "status": "requeued"}

    @app.get("/runs/{run_id}/events")
    async def run_events(run_id: uuid.UUID, request: Request, caller: Caller
                         ) -> StreamingResponse:  # fmt: skip
        d = get_deps(request)
        viewer = _viewer_id(caller)
        if await load_run_view(d.sessions, run_id, viewer) is None:
            raise HTTPException(404, "run not found")

        async def stream() -> AsyncIterator[str]:
            # Polls the DB rather than a pub/sub channel: the DB is the source of truth,
            # so a reconnecting client always gets the real current state.
            loop = asyncio.get_running_loop()
            started = last_beat = loop.time()
            last: str | None = None
            while loop.time() - started < SSE_MAX_SECONDS:
                view = await load_run_view(d.sessions, run_id, viewer)
                assert view is not None
                payload = view.model_dump_json()
                if payload != last:
                    last, last_beat = payload, loop.time()
                    yield f"event: update\ndata: {payload}\n\n"
                if view.status in {s.value for s in TERMINAL}:
                    yield f"event: done\ndata: {json.dumps({'status': view.status})}\n\n"
                    return
                if loop.time() - last_beat > SSE_HEARTBEAT_SECONDS:
                    last_beat = loop.time()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(d.poll_seconds)
            yield "event: timeout\ndata: {}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})  # fmt: skip

    return app


app = create_app()
