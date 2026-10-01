"""Orchestrator engine tests: fake steps on a temp SQLite database. No API calls."""

import asyncio
import datetime as dt
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.db.models import Base, Run, RunStatus, StepStatus
from app.db.session import make_engine, make_sessionmaker
from app.pipeline.orchestrator import Orchestrator, StepContext, StepResult
from tests.samples import LEAD, SENDER


@pytest.fixture
async def sessions(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'o.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield make_sessionmaker(engine)
    await engine.dispose()


class Steps:
    """Fake pipeline: counts calls; `fail_on` makes a step raise until fixed."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.fail_on: str | None = None
        self.stop_on: str | None = None

    def make(self, name: str, cost: float = 0.01):
        async def fn(ctx: StepContext) -> StepResult:
            self.calls[name] += 1
            if name == self.fail_on:
                raise RuntimeError(f"{name} broke")
            # Each step sees everything earlier steps produced.
            return StepResult(
                output={"step": name, "saw": sorted(ctx.outputs)},
                cost_usd=cost,
                stop="duplicate lead" if name == self.stop_on else None,
            )

        return fn

    def pipeline(self):
        return [(n, self.make(n)) for n in ("validate", "dedupe", "research", "draft")]


async def load(sessions, run_id) -> Run:
    async with sessions() as s:
        return (await s.execute(select(Run).where(Run.id == run_id)
                                .options(selectinload(Run.steps)))).scalar_one()  # fmt: skip


async def test_happy_path_persists_everything(sessions) -> None:
    steps = Steps()
    orch = Orchestrator(sessions, steps.pipeline())
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.SUCCEEDED

    run = await load(sessions, run_id)
    assert [s.status for s in run.steps] == [StepStatus.SUCCEEDED] * 4
    assert run.steps[3].output == {"step": "draft", "saw": ["dedupe", "research", "validate"]}
    assert run.cost_usd == pytest.approx(0.04)
    assert run.sender["name"] == SENDER.name  # snapshot for resumes


async def test_failed_step_resumes_without_rerunning_finished_steps(sessions) -> None:
    steps = Steps()
    steps.fail_on = "research"
    orch = Orchestrator(sessions, steps.pipeline())
    run_id = await orch.create_run(LEAD, SENDER)

    assert await orch.execute(run_id) is RunStatus.FAILED
    run = await load(sessions, run_id)
    assert [s.status for s in run.steps] == [
        StepStatus.SUCCEEDED, StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.PENDING,
    ]  # fmt: skip
    assert "research broke" in (run.error or "") and run.current_step == "research"

    steps.fail_on = None  # "fix" the problem, then resume
    assert await orch.execute(run_id) is RunStatus.SUCCEEDED
    assert steps.calls == {"validate": 1, "dedupe": 1, "research": 2, "draft": 1}
    run = await load(sessions, run_id)
    assert run.steps[2].attempts == 2 and run.steps[2].error is None
    # draft still received the earlier outputs, restored from the database
    assert run.steps[3].output["saw"] == ["dedupe", "research", "validate"]


async def test_stop_ends_run_early_without_failing(sessions) -> None:
    steps = Steps()
    steps.stop_on = "dedupe"
    orch = Orchestrator(sessions, steps.pipeline())
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.STOPPED
    run = await load(sessions, run_id)
    assert run.error == "duplicate lead" and "research" not in steps.calls
    assert await orch.execute(run_id) is RunStatus.STOPPED  # stopped runs don't restart


async def test_last_step_sets_final_status(sessions) -> None:
    async def draft(ctx: StepContext) -> StepResult:
        return StepResult({"status": "needs_human"}, run_status=RunStatus.NEEDS_HUMAN)

    orch = Orchestrator(sessions, [("draft", draft)])
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.NEEDS_HUMAN


async def test_concurrent_execute_runs_steps_once(sessions) -> None:
    calls = Counter()

    async def slow(ctx: StepContext) -> StepResult:
        calls["slow"] += 1
        await asyncio.sleep(0.05)
        return StepResult({})

    orch = Orchestrator(sessions, [("slow", slow)])
    run_id = await orch.create_run(LEAD, SENDER)
    results = await asyncio.gather(orch.execute(run_id), orch.execute(run_id))
    assert calls["slow"] == 1  # the second worker lost the claim
    assert RunStatus.SUCCEEDED in results


async def test_stale_running_run_can_be_reclaimed(sessions) -> None:
    steps = Steps()
    orch = Orchestrator(sessions, steps.pipeline(), stale_after=dt.timedelta(minutes=15))
    run_id = await orch.create_run(LEAD, SENDER)
    async with sessions() as s:  # simulate a worker that died mid-run
        await s.execute(update(Run).where(Run.id == run_id).values(status=RunStatus.RUNNING))
        await s.commit()
    assert await orch.execute(run_id) is RunStatus.RUNNING  # fresh: leave it alone
    assert not steps.calls

    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    async with sessions() as s:
        await s.execute(update(Run).where(Run.id == run_id).values(updated_at=long_ago))
        await s.commit()
    assert await orch.execute(run_id) is RunStatus.SUCCEEDED  # stale: reclaimed


def test_step_names_must_be_unique(sessions) -> None:
    async def f(ctx):
        return StepResult({})

    with pytest.raises(ValueError, match="unique"):
        Orchestrator(sessions, [("a", f), ("a", f)])
