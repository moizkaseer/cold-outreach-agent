"""Enqueue pipeline runs. Used by the API (and scripts)."""

from __future__ import annotations

import uuid

from arq.connections import ArqRedis


def job_id_for(run_id: uuid.UUID, attempt: int = 0) -> str:
    # A deterministic job id makes enqueueing idempotent: Arq ignores a second enqueue
    # with the same id while the first is queued/running, so a double-clicked "Run" or
    # a retried API request can't start the pipeline twice.
    return f"run:{run_id}:{attempt}"


async def enqueue_run(redis: ArqRedis, run_id: uuid.UUID, attempt: int = 0) -> bool:
    """Returns False if an identical job was already queued."""
    job = await redis.enqueue_job("run_pipeline", str(run_id), _job_id=job_id_for(run_id, attempt))
    return job is not None
