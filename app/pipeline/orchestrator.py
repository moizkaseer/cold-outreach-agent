"""Pipeline orchestrator: a plain state machine over persisted steps.

Code controls the flow; steps are just named async functions. Every step's status,
output, error and cost is saved under the run's id BEFORE moving on, so:
- a crashed or failed run resumes from the failed step (finished steps are skipped and
  their saved outputs reused, so nothing is paid for twice);
- every run can be inspected step by step afterwards.

Runs are "claimed" with a conditional UPDATE before executing, so two workers can't
process the same run at once. A run left RUNNING by a dead worker becomes claimable again
after `stale_after`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.db.models import Lead as LeadRow
from app.db.models import Run, RunStatus, Step, StepStatus, as_utc, utcnow
from app.schemas import Lead, Sender

STALE_AFTER = dt.timedelta(minutes=15)  # longer than any single step should take


@dataclass
class StepContext:
    run_id: uuid.UUID
    lead: Lead
    sender: Sender
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)  # earlier steps


@dataclass
class StepResult:
    output: dict[str, Any]  # must be JSON-serialisable: it is persisted
    cost_usd: float = 0.0
    stop: str | None = None  # end the run early (not a failure), with this reason
    run_status: RunStatus | None = None  # final status override (last step decides)


StepFn = Callable[[StepContext], Awaitable[StepResult]]


class Orchestrator:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        steps: list[tuple[str, StepFn]],
        *,
        stale_after: dt.timedelta = STALE_AFTER,
    ) -> None:
        names = [n for n, _ in steps]
        if len(names) != len(set(names)):
            raise ValueError("step names must be unique")
        self._sessions = sessions
        self._steps = steps
        self._stale_after = stale_after

    async def create_run(self, lead: Lead, sender: Sender) -> uuid.UUID:
        async with self._sessions() as s:
            row = LeadRow(domain=lead.domain, person_name=lead.person_name,
                          person_role=lead.person_role)  # fmt: skip
            run = Run(lead=row, sender=sender.model_dump(mode="json"))
            run.steps = [Step(name=n, position=i) for i, (n, _) in enumerate(self._steps, 1)]
            s.add(run)
            await s.commit()
            return run.id

    async def _claim(self, run_id: uuid.UUID) -> bool:
        """Atomically move the run to RUNNING if it is runnable. Optimistic lock: the
        UPDATE only matches if status and updated_at are what we just read."""
        async with self._sessions() as s:
            run = await s.get(Run, run_id)
            if run is None:
                raise KeyError(f"run {run_id} not found")
            runnable = run.status in (RunStatus.PENDING, RunStatus.FAILED) or (
                run.status is RunStatus.RUNNING
                and utcnow() - as_utc(run.updated_at) > self._stale_after
            )
            if not runnable:
                return False
            result = await s.execute(
                update(Run)
                .where(
                    Run.id == run_id, Run.status == run.status, Run.updated_at == run.updated_at
                )  # fmt: skip
                .values(status=RunStatus.RUNNING, error=None, updated_at=utcnow())
            )
            await s.commit()
            return result.rowcount == 1

    async def execute(self, run_id: uuid.UUID) -> RunStatus:
        """Run (or resume) a run. Returns its final status; never raises for step errors."""
        if not await self._claim(run_id):
            async with self._sessions() as s:
                run = await s.get(Run, run_id)
                assert run is not None
                return run.status  # someone else has it, or it's already finished

        async with self._sessions() as s:
            run = (
                await s.execute(
                    select(Run)
                    .where(Run.id == run_id)
                    .options(selectinload(Run.steps), selectinload(Run.lead))
                )
            ).scalar_one()
            ctx = StepContext(
                run_id=run.id,
                lead=Lead(
                    domain=run.lead.domain,
                    person_name=run.lead.person_name,
                    person_role=run.lead.person_role,
                ),  # fmt: skip
                sender=Sender.model_validate(run.sender),
            )
            rows = {step.name: step for step in run.steps}
            final = RunStatus.SUCCEEDED

            for name, fn in self._steps:
                step = rows[name]
                if step.status is StepStatus.SUCCEEDED:  # resume: reuse saved output
                    ctx.outputs[name] = step.output or {}
                    continue

                step.status, step.error = StepStatus.RUNNING, None
                step.attempts += 1
                step.started_at, step.finished_at = utcnow(), None
                run.current_step = name
                await s.commit()  # persist "running" before doing any work

                try:
                    result = await fn(ctx)
                except Exception as e:  # any step error: save it, fail the run, stop
                    step.status, step.error = StepStatus.FAILED, f"{type(e).__name__}: {e}"
                    step.finished_at = utcnow()
                    run.status, run.error = RunStatus.FAILED, f"step '{name}' failed: {e}"
                    await s.commit()
                    return RunStatus.FAILED

                step.status, step.output = StepStatus.SUCCEEDED, result.output
                step.cost_usd += result.cost_usd  # across attempts: real money spent
                step.finished_at = utcnow()
                run.cost_usd += result.cost_usd
                ctx.outputs[name] = result.output
                if result.run_status is not None:
                    final = result.run_status
                if result.stop is not None:
                    run.status, run.error = RunStatus.STOPPED, result.stop
                    await s.commit()
                    return RunStatus.STOPPED
                await s.commit()

            run.status, run.current_step = final, None
            await s.commit()
            return final
