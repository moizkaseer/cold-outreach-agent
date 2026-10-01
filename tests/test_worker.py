"""Worker wiring tests. No Redis needed: the job function and enqueue are tested directly
with fakes. A real Redis round-trip is NOT covered here."""

import uuid

from app.db.models import RunStatus
from app.workers.queue import enqueue_run, job_id_for
from app.workers.worker import JOB_TIMEOUT_S, WorkerSettings, run_pipeline


class FakeOrchestrator:
    def __init__(self) -> None:
        self.executed: list[uuid.UUID] = []

    async def execute(self, run_id: uuid.UUID) -> RunStatus:
        self.executed.append(run_id)
        return RunStatus.NEEDS_HUMAN


async def test_job_executes_the_run() -> None:
    orch = FakeOrchestrator()
    rid = uuid.uuid4()
    assert await run_pipeline({"orchestrator": orch}, str(rid)) == "needs_human"
    assert orch.executed == [rid]


class FakeRedis:
    def __init__(self) -> None:
        self.jobs: dict[str, tuple] = {}

    async def enqueue_job(self, function, *args, _job_id=None):
        if _job_id in self.jobs:  # Arq's behaviour for a duplicate job id
            return None
        self.jobs[_job_id] = (function, args)
        return object()


async def test_enqueue_is_idempotent_per_attempt() -> None:
    redis, rid = FakeRedis(), uuid.uuid4()
    assert await enqueue_run(redis, rid) is True  # type: ignore[arg-type]
    assert await enqueue_run(redis, rid) is False  # type: ignore[arg-type]  # double click
    assert await enqueue_run(redis, rid, attempt=1) is True  # type: ignore[arg-type]  # resume
    assert redis.jobs[job_id_for(rid)] == ("run_pipeline", (str(rid),))


def test_worker_settings() -> None:
    assert run_pipeline in WorkerSettings.functions
    assert WorkerSettings.job_timeout == JOB_TIMEOUT_S and WorkerSettings.max_tries == 1
