"""The real pipeline steps (CLAUDE.md architecture):

    validate -> dedupe -> score (quality gate) -> research -> painpoints -> draft

Cheap checks run first so money is only spent on leads worth it. Each step returns a
JSON-able output that the orchestrator persists; later steps read earlier outputs from
`ctx.outputs`, which is what makes resume-from-failed-step work. "Stop" means the run
ended on purpose (duplicate, poor fit, nothing to say); "StepFailed" means something broke
and the run can be resumed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.drafting import DraftResult, DraftStatus
from app.agents.painpoints import PainPointRun
from app.agents.research import ResearchRun
from app.agents.scorer import ScoreRun
from app.db.models import (
    EmailDraft,
    EmailStatus,
    ResearchCacheEntry,
    Run,
    RunStatus,
    as_utc,
    utcnow,
)
from app.db.models import Lead as LeadRow
from app.pipeline.orchestrator import StepContext, StepFailed, StepFn, StepResult
from app.pricing import total_cost_usd
from app.schemas import Lead, PainPoint, PainPoints, ResearchReport, Sender

ScoreFn = Callable[[Lead, Sender], Awaitable[ScoreRun]]
ResearchFn = Callable[[Lead], Awaitable[ResearchRun]]
PainPointsFn = Callable[[Lead, ResearchReport, Sender], Awaitable[PainPointRun]]
DraftFn = Callable[[Lead, ResearchReport, PainPoint, Sender], Awaitable[DraftResult]]

ACTIVE = (RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.NEEDS_HUMAN)

# Order matters: cheap gates first. Must match build_pipeline (a test enforces this).
STEP_NAMES = ["validate", "dedupe", "score", "research", "painpoints", "draft"]


@dataclass
class PipelineDeps:
    sessions: async_sessionmaker[AsyncSession]
    score: ScoreFn
    research: ResearchFn
    pain_points: PainPointsFn
    draft: DraftFn
    min_score: int = 3
    cache_ttl: dt.timedelta = dt.timedelta(days=7)  # CLAUDE.md
    dedupe_window: dt.timedelta = dt.timedelta(days=30)


def _cost(*agents: Any, extra: float = 0.0) -> float:
    # Unknown model prices give None; configured models are always priced (app/pricing.py).
    return (total_cost_usd(a for a in agents if a is not None) or 0.0) + extra


def build_pipeline(d: PipelineDeps) -> list[tuple[str, StepFn]]:
    async def validate(ctx: StepContext) -> StepResult:
        lead = Lead.model_validate(ctx.lead.model_dump())  # re-normalise, fail loudly
        if ctx.sender.is_placeholder:
            return StepResult({"lead": lead.model_dump()},
                              stop="sender profile is the example placeholder")  # fmt: skip
        return StepResult({"lead": lead.model_dump()})

    async def dedupe(ctx: StepContext) -> StepResult:
        since = utcnow() - d.dedupe_window
        async with d.sessions() as s:
            rows = await s.execute(
                select(Run.id, Run.created_at)
                .join(LeadRow, Run.lead_id == LeadRow.id)
                .where(
                    LeadRow.domain == ctx.lead.domain,
                    func.lower(LeadRow.person_name) == ctx.lead.person_name.lower(),
                    Run.id != ctx.run_id,
                    Run.status.in_(ACTIVE),
                )
            )
            # Compare in Python: SQLite returns naive datetimes, Postgres aware ones.
            dupes = [str(rid) for rid, created in rows if as_utc(created) >= since]
        if dupes:
            return StepResult({"duplicates": dupes}, stop=f"duplicate of run {dupes[0]}")
        return StepResult({"duplicates": []})

    async def score(ctx: StepContext) -> StepResult:
        run = await d.score(ctx.lead, ctx.sender)
        cost = _cost(run.agent)
        if run.score is None:
            raise StepFailed(f"scorer failed: {run.agent.stop_reason}", cost_usd=cost)
        out = run.score.model_dump()
        if run.score.score < d.min_score:
            return StepResult(out, cost, stop=f"low fit {run.score.score}/5: {run.score.reason}")
        return StepResult(out, cost)

    async def research(ctx: StepContext) -> StepResult:
        async with d.sessions() as s:
            entry = await s.get(ResearchCacheEntry, ctx.lead.domain)
            if entry is not None and as_utc(entry.expires_at) > utcnow():
                report = ResearchReport.model_validate(entry.report)
                return StepResult({"report": report.model_dump(mode="json"), "cached": True,
                                   "rejected": 0})  # fmt: skip

        run = await d.research(ctx.lead)
        cost = _cost(run.agent, extra=run.search_cost_usd)
        if run.report is None:
            raise StepFailed(f"research failed: {run.agent.stop_reason}", cost_usd=cost)
        out = {"report": run.report.model_dump(mode="json"), "cached": False,
               "rejected": len(run.rejected)}  # fmt: skip
        if not run.report.facts:
            return StepResult(out, cost, stop="research found no grounded facts")

        async with d.sessions() as s:  # cache only useful reports
            await s.merge(ResearchCacheEntry(
                domain=ctx.lead.domain, report=out["report"],
                created_at=utcnow(), expires_at=utcnow() + d.cache_ttl,
            ))  # fmt: skip
            await s.commit()
        return StepResult(out, cost)

    async def painpoints(ctx: StepContext) -> StepResult:
        report = ResearchReport.model_validate(ctx.outputs["research"]["report"])
        run = await d.pain_points(ctx.lead, report, ctx.sender)
        cost = _cost(run.agent)
        if run.pain_points is None:
            if run.agent.output is None:  # the model failed: worth retrying
                raise StepFailed(f"pain points failed: {run.agent.stop_reason}", cost_usd=cost)
            return StepResult({"dropped": len(run.dropped)}, cost,
                              stop="no pain points grounded in the research")  # fmt: skip
        return StepResult({"pain_points": run.pain_points.model_dump(mode="json"),
                           "dropped": len(run.dropped)}, cost)  # fmt: skip

    async def draft(ctx: StepContext) -> StepResult:
        report = ResearchReport.model_validate(ctx.outputs["research"]["report"])
        top = PainPoints.model_validate(ctx.outputs["painpoints"]["pain_points"]).top
        result = await d.draft(ctx.lead, report, top, ctx.sender)
        cost = _cost(*(a for att in result.attempts for a in att.agents))
        if result.status is DraftStatus.FAILED or result.email is None:
            raise StepFailed(f"drafting failed: {result.reason}", cost_usd=cost)

        verified = result.status is DraftStatus.VERIFIED
        problems = result.attempts[-1].problems if result.attempts else []
        async with d.sessions() as s:
            row = (await s.execute(select(EmailDraft).where(EmailDraft.run_id == ctx.run_id))
                   ).scalar_one_or_none() or EmailDraft(run_id=ctx.run_id)  # fmt: skip
            row.subject, row.body = result.email.subject, result.email.body
            row.used_fact_ids = result.email.used_fact_ids
            row.status = EmailStatus.VERIFIED if verified else EmailStatus.NEEDS_HUMAN
            row.verification = (result.verification.model_dump(mode="json")
                                if result.verification else None)  # fmt: skip
            row.problems = problems
            s.add(row)
            await s.commit()

        return StepResult(
            {"status": result.status.value, "revisions": result.revisions,
             "email": result.email.model_dump(mode="json"), "problems": problems},
            cost,
            run_status=RunStatus.SUCCEEDED if verified else RunStatus.NEEDS_HUMAN,
        )  # fmt: skip

    return [
        ("validate", validate),
        ("dedupe", dedupe),
        ("score", score),
        ("research", research),
        ("painpoints", painpoints),
        ("draft", draft),
    ]
