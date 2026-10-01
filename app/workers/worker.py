"""Arq worker: runs pipelines in the background (agent runs take 30-120s+; never run
them inside a web request).

    uv run arq app.workers.worker.WorkerSettings

Each job just calls `Orchestrator.execute(run_id)`. All durability lives in the database,
not in Redis: if a worker dies, the run stays resumable from its last finished step, and
the orchestrator's claim stops two workers from running the same run.
"""

from __future__ import annotations

import uuid
from typing import Any

from arq.connections import RedisSettings

from app.config import InfraSettings, get_settings
from app.db.session import make_engine, make_sessionmaker
from app.pipeline.factory import build_orchestrator

JOB_TIMEOUT_S = 600  # a full pipeline incl. 2 revisions; well above the expected 1-4 min
MAX_JOBS = 4  # concurrent pipelines per worker: keeps API rate limits and spend in check


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    ctx["engine"] = engine
    ctx["orchestrator"] = build_orchestrator(settings, make_sessionmaker(engine))


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["engine"].dispose()


async def run_pipeline(ctx: dict[str, Any], run_id: str) -> str:
    """Execute (or resume) one run. Never raises for step failures: those are saved on the
    run, so Arq's own retries aren't needed; resuming is an explicit action."""
    status = await ctx["orchestrator"].execute(uuid.UUID(run_id))
    return status.value


class WorkerSettings:
    functions = [run_pipeline]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(InfraSettings().redis_url)
    job_timeout = JOB_TIMEOUT_S
    max_jobs = MAX_JOBS
    max_tries = 1  # see run_pipeline: failures are persisted and resumed explicitly
