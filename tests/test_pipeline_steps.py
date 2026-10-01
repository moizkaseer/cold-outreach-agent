"""Full pipeline with every agent faked, on a temp SQLite DB. No API calls."""

import datetime as dt
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.agents.drafting import DraftAttempt, DraftResult, DraftStatus
from app.agents.loop import AgentResult, StopReason, Usage
from app.agents.painpoints import PainPointRun
from app.agents.research import ResearchRun
from app.agents.scorer import LeadScore, ScoreRun
from app.db.models import (
    Base,
    EmailDraft,
    EmailStatus,
    ResearchCacheEntry,
    Run,
    RunStatus,
    StepStatus,
)  # fmt: skip
from app.db.session import make_engine, make_sessionmaker
from app.pipeline.orchestrator import Orchestrator
from app.pipeline.steps import PipelineDeps, build_pipeline
from app.schemas import Email, Lead, PainPoints, Sender
from tests.samples import LEAD, REPORT, SENDER
from tests.test_writer import GOOD_BODY, PAIN

SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-4-5"
EMAIL = Email(subject="s", body=GOOD_BODY, used_fact_ids=["f1"])


def agent(model: str, output=True) -> AgentResult:
    return AgentResult(output, StopReason.FINISHED if output else StopReason.MAX_TURNS, 1,
                       Usage(input_tokens=10_000), model=model)  # fmt: skip


class Fakes:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.score_value = 4
        self.report = REPORT
        self.research_fails = False
        self.draft_status = DraftStatus.VERIFIED

    async def score(self, lead, sender):
        self.calls["score"] += 1
        return ScoreRun(LeadScore(score=self.score_value, reason="r"), agent(HAIKU))

    async def research(self, lead):
        self.calls["research"] += 1
        if self.research_fails:
            return ResearchRun(None, agent(SONNET, output=None), search_cost_usd=0.01)
        return ResearchRun(self.report, agent(SONNET), search_cost_usd=0.01)

    async def pain_points(self, lead, report, sender):
        self.calls["pain_points"] += 1
        return PainPointRun(PainPoints(pain_points=[PAIN]), agent(SONNET))

    async def draft(self, lead, report, pp, sender):
        self.calls["draft"] += 1
        problems = [] if self.draft_status is DraftStatus.VERIFIED else ["claim X unsupported"]
        return DraftResult(
            self.draft_status,
            EMAIL,
            None,
            [DraftAttempt(EMAIL, problems, agents=[agent(SONNET), agent(HAIKU)])],
        )


@pytest.fixture
async def env(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = make_sessionmaker(engine)
    fakes = Fakes()
    deps = PipelineDeps(sessions=sessions, score=fakes.score, research=fakes.research,
                        pain_points=fakes.pain_points, draft=fakes.draft)  # fmt: skip
    yield Orchestrator(sessions, build_pipeline(deps)), sessions, fakes
    await engine.dispose()


async def load(sessions, run_id) -> Run:
    async with sessions() as s:
        return (await s.execute(select(Run).where(Run.id == run_id).options(
            selectinload(Run.steps), selectinload(Run.email)))).scalar_one()  # fmt: skip


async def test_happy_path(env) -> None:
    orch, sessions, fakes = env
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.SUCCEEDED

    run = await load(sessions, run_id)
    assert [s.name for s in run.steps] == [
        "validate", "dedupe", "score", "research", "painpoints", "draft"]  # fmt: skip
    assert run.email.status is EmailStatus.VERIFIED and run.email.subject == "s"
    # 10k input tokens per agent: haiku 0.01, research sonnet 0.02 + 0.01 search,
    # painpoints 0.02, draft sonnet 0.02 + haiku 0.01
    assert run.cost_usd == pytest.approx(0.01 + 0.03 + 0.02 + 0.03)
    async with sessions() as s:
        assert await s.get(ResearchCacheEntry, "acme.com") is not None


async def test_research_cache_shared_across_leads(env) -> None:
    orch, sessions, fakes = env
    await orch.execute(await orch.create_run(LEAD, SENDER))
    other = Lead(domain="acme.com", person_name="Bo Chen", person_role="COO")
    run_id = await orch.create_run(other, SENDER)
    assert await orch.execute(run_id) is RunStatus.SUCCEEDED
    assert fakes.calls["research"] == 1  # second lead reused the cache
    run = await load(sessions, run_id)
    assert run.steps[3].output["cached"] is True and run.steps[3].cost_usd == 0


async def test_expired_cache_is_refreshed(env) -> None:
    orch, sessions, fakes = env
    await orch.execute(await orch.create_run(LEAD, SENDER))
    async with sessions() as s:
        await s.execute(update(ResearchCacheEntry).values(
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1)))  # fmt: skip
        await s.commit()
    other = Lead(domain="acme.com", person_name="Bo Chen", person_role="COO")
    await orch.execute(await orch.create_run(other, SENDER))
    assert fakes.calls["research"] == 2


async def test_duplicate_lead_stops(env) -> None:
    orch, sessions, fakes = env
    await orch.execute(await orch.create_run(LEAD, SENDER))
    again = Lead(domain="acme.com", person_name="ANA SILVA", person_role="Head of People")
    run_id = await orch.create_run(again, SENDER)
    assert await orch.execute(run_id) is RunStatus.STOPPED
    assert "duplicate" in ((await load(sessions, run_id)).error or "")
    assert fakes.calls["score"] == 1  # stopped before paying for anything


async def test_low_score_stops_before_research(env) -> None:
    orch, sessions, fakes = env
    fakes.score_value = 2
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.STOPPED
    run = await load(sessions, run_id)
    assert "low fit 2/5" in (run.error or "") and fakes.calls["research"] == 0
    assert run.cost_usd == pytest.approx(0.01)  # only the scorer was paid


async def test_placeholder_sender_stops_at_validate(env) -> None:
    orch, sessions, fakes = env
    placeholder = Sender(name="Your Name", company="c", offer="o", outcomes=["x"])
    run_id = await orch.create_run(LEAD, placeholder)
    assert await orch.execute(run_id) is RunStatus.STOPPED
    assert sum(fakes.calls.values()) == 0


async def test_research_failure_records_cost_and_resumes(env) -> None:
    orch, sessions, fakes = env
    fakes.research_fails = True
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.FAILED
    run = await load(sessions, run_id)
    assert run.steps[3].status is StepStatus.FAILED
    assert run.steps[3].cost_usd == pytest.approx(0.03)  # spent before failing: recorded

    fakes.research_fails = False
    assert await orch.execute(run_id) is RunStatus.SUCCEEDED
    assert fakes.calls["score"] == 1  # finished steps were not re-run (or re-paid)
    assert (await load(sessions, run_id)).cost_usd == pytest.approx(0.01 + 0.03 * 2 + 0.05)


async def test_needs_human_draft(env) -> None:
    orch, sessions, fakes = env
    fakes.draft_status = DraftStatus.NEEDS_HUMAN
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.NEEDS_HUMAN
    run = await load(sessions, run_id)
    assert run.email.status is EmailStatus.NEEDS_HUMAN
    assert run.email.problems == ["claim X unsupported"]


async def test_no_facts_stops_and_is_not_cached(env) -> None:
    orch, sessions, fakes = env
    fakes.report = REPORT.model_copy(update={"facts": []})
    run_id = await orch.create_run(LEAD, SENDER)
    assert await orch.execute(run_id) is RunStatus.STOPPED
    async with sessions() as s:
        assert await s.get(ResearchCacheEntry, "acme.com") is None
        assert (await s.execute(select(EmailDraft))).first() is None


def test_factory_builds_without_calling_apis(tmp_path: Path) -> None:
    from app.config import Settings
    from app.pipeline.factory import build_orchestrator

    s = Settings(_env_file=None, anthropic_api_key="x", exa_api_key="y")
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'f.db'}")
    orch = build_orchestrator(s, make_sessionmaker(engine))
    assert [n for n, _ in orch._steps][-1] == "draft"
