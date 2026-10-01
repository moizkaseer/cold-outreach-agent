"""Wire the real agents into the pipeline. The worker and API use this."""

from __future__ import annotations

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.drafting import DraftResult, draft_email
from app.agents.painpoints import PainPointAgent, PainPointRun
from app.agents.research import ResearchAgent
from app.agents.scorer import LeadScorer, ScoreRun
from app.agents.verifier import VerifierAgent
from app.agents.writer import WriterAgent
from app.config import Settings
from app.pipeline.orchestrator import Orchestrator
from app.pipeline.steps import PipelineDeps, build_pipeline
from app.schemas import Lead, PainPoint, ResearchReport, Sender


def build_orchestrator(s: Settings, sessions: async_sessionmaker[AsyncSession]) -> Orchestrator:
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
    scorer = LeadScorer(client=client, model=s.model_fast)  # Haiku: scoring
    researcher = ResearchAgent.from_settings(s)  # Sonnet: research
    pp_agent = PainPointAgent(client=client, model=s.model_smart)
    writer = WriterAgent(client=client, model=s.model_smart)  # Sonnet: writing
    verifier = VerifierAgent(client=client, model=s.model_fast)  # Haiku: verification

    async def score(lead: Lead, sender: Sender) -> ScoreRun:
        return await scorer.score(lead, sender)

    async def pain_points(lead: Lead, report: ResearchReport, sender: Sender) -> PainPointRun:
        return await pp_agent.run(lead, report, sender)

    async def draft(lead: Lead, report: ResearchReport, pp: PainPoint, sender: Sender
                    ) -> DraftResult:  # fmt: skip
        return await draft_email(writer=writer, verifier=verifier, lead=lead, report=report,
                                 pain_point=pp, sender=sender)  # fmt: skip

    deps = PipelineDeps(sessions=sessions, score=score, research=researcher.run,
                        pain_points=pain_points, draft=draft)  # fmt: skip
    return Orchestrator(sessions, build_pipeline(deps))
