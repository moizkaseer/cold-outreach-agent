"""Writer agent: drafts one email from research + top pain point + sender profile.

No tools, only `finish`. Schema rules (word limit) are retried by the loop; the other
code-checkable rules come back as `problems` for the revise loop to act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import anthropic

from app.agents.formatting import facts_block, sender_block
from app.agents.loop import AgentResult, run_agent_loop
from app.agents.writer.prompts import REVISION_TEMPLATE, SYSTEM_PROMPT, USER_TEMPLATE
from app.agents.writer.rules import lint_email
from app.schemas import Email, Lead, PainPoint, ResearchReport, Sender

MAX_TURNS = 3
TOKEN_BUDGET = 60_000


@dataclass(frozen=True)
class Revision:
    """Feedback for a rewrite: the rejected draft and what was wrong with it."""

    attempt: int  # 1, 2, ...
    previous: Email
    problems: list[str]


@dataclass
class WriterRun:
    email: Email | None
    agent: AgentResult[Email]
    problems: list[str] = field(default_factory=list)  # code-checked rule violations


class WriterAgent:
    def __init__(self, *, client: anthropic.AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def write(
        self,
        lead: Lead,
        report: ResearchReport,
        pain_point: PainPoint,
        sender: Sender,
        revision: Revision | None = None,
    ) -> WriterRun:
        user = USER_TEMPLATE.format(
            person_name=lead.person_name,
            person_role=lead.person_role,
            company=report.company_name,
            sender=sender_block(sender),
            pp_title=pain_point.title,
            pp_rationale=pain_point.rationale,
            pp_fact_ids=", ".join(pain_point.supporting_fact_ids),
            facts=facts_block(report),
        )
        if revision is not None:
            user += REVISION_TEMPLATE.format(
                attempt=revision.attempt,
                subject=revision.previous.subject,
                body=revision.previous.body,
                problems="\n".join(f"- {p}" for p in revision.problems),
            )

        agent = await run_agent_loop(
            name="writer",
            client=self._client,
            model=self._model,
            system=SYSTEM_PROMPT,
            user_message=user,
            tools=[],
            output_model=Email,
            max_turns=MAX_TURNS,
            token_budget=TOKEN_BUDGET,
        )
        if agent.output is None:
            return WriterRun(None, agent)
        return WriterRun(agent.output, agent, lint_email(agent.output, report, sender))
