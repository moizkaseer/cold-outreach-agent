"""Pain point agent: one reasoning step after research (sequential, not in parallel).

No tools, only `finish`. After the model answers, code drops any pain point that cites a
fact id not in the report (ungrounded), and re-ranks the rest 1..N.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import anthropic

from app.agents.formatting import facts_block, sender_block
from app.agents.loop import AgentResult, run_agent_loop
from app.agents.painpoints.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from app.schemas import Lead, PainPoint, PainPoints, ResearchReport, Sender

MAX_TURNS = 3  # one answer + one retry on invalid output + slack
TOKEN_BUDGET = 60_000


@dataclass
class PainPointRun:
    pain_points: PainPoints | None
    agent: AgentResult[PainPoints]
    dropped: list[PainPoint] = field(default_factory=list)


def ground_pain_points(
    pps: PainPoints, report: ResearchReport
) -> tuple[PainPoints | None, list[PainPoint]]:
    """Keep pain points whose every supporting fact id exists in the report; re-rank."""
    known = report.fact_ids()
    kept = [p for p in pps.pain_points if set(p.supporting_fact_ids) <= known]
    dropped = [p for p in pps.pain_points if not set(p.supporting_fact_ids) <= known]
    if not kept:
        return None, dropped
    reranked = [p.model_copy(update={"rank": i}) for i, p in enumerate(kept, 1)]
    return PainPoints(pain_points=reranked), dropped


class PainPointAgent:
    def __init__(self, *, client: anthropic.AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def run(self, lead: Lead, report: ResearchReport, sender: Sender) -> PainPointRun:
        if not report.facts:
            raise ValueError("cannot infer pain points from a report with no facts")
        agent = await run_agent_loop(
            name="painpoints",
            client=self._client,
            model=self._model,
            system=SYSTEM_PROMPT,
            user_message=USER_TEMPLATE.format(
                person_name=lead.person_name,
                person_role=lead.person_role,
                company=report.company_name,
                sender=sender_block(sender),
                facts=facts_block(report),
            ),
            tools=[],
            output_model=PainPoints,
            max_turns=MAX_TURNS,
            token_budget=TOKEN_BUDGET,
        )
        if agent.output is None:
            return PainPointRun(None, agent)
        grounded, dropped = ground_pain_points(agent.output, report)
        return PainPointRun(grounded, agent, dropped)
