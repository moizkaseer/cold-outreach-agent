"""Lead scorer: cheap quality gate (Haiku) before paying for research."""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
from pydantic import BaseModel, Field

from app.agents.loop import AgentResult, run_agent_loop
from app.agents.scorer.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from app.schemas import Lead, Sender


class LeadScore(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str = Field(description="One short sentence.")


@dataclass
class ScoreRun:
    score: LeadScore | None
    agent: AgentResult[LeadScore]


class LeadScorer:
    def __init__(self, *, client: anthropic.AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def score(self, lead: Lead, sender: Sender) -> ScoreRun:
        agent = await run_agent_loop(
            name="scorer",
            client=self._client,
            model=self._model,
            system=SYSTEM_PROMPT,
            user_message=USER_TEMPLATE.format(
                domain=lead.domain, person_role=lead.person_role, offer=sender.offer
            ),
            tools=[],
            output_model=LeadScore,
            max_turns=3,
            token_budget=20_000,
            refusal_fallback=False,  # Haiku
        )
        return ScoreRun(agent.output, agent)
