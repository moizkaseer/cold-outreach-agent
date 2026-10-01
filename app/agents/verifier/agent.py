"""Verifier: checks every factual claim in an email against research quotes and the
sender's outcomes.

The model only maps each claim to a source (fact + quote number, or sender outcome). CODE
decides whether that source exists and builds the `Verification` and the writer feedback,
so the model can't mark a claim "supported" by pointing at nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
from pydantic import BaseModel, Field

from app.agents.formatting import facts_block
from app.agents.loop import AgentResult, run_agent_loop
from app.agents.verifier.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from app.schemas import ClaimCheck, Email, ResearchReport, Sender, Verification

MAX_TURNS = 3
TOKEN_BUDGET = 60_000


class ClaimSource(BaseModel):
    claim: str = Field(description="The factual claim, as written in the email.")
    fact_id: str | None = Field(description="Research fact id that states it, or null.")
    quote: int | None = Field(description="1-based quote number within that fact, or null.")
    outcome: int | None = Field(description="1-based sender outcome number, or null.")
    reason: str = Field(description="One short sentence.")


class VerifierOutput(BaseModel):
    claims: list[ClaimSource]


@dataclass
class VerifyRun:
    verification: Verification | None
    agent: AgentResult[VerifierOutput]
    error: str | None = None


def resolve(src: ClaimSource, report: ResearchReport, sender: Sender) -> ClaimCheck:
    """A claim is supported only if it points at a quote or outcome that really exists."""
    facts = {f.id: f for f in report.facts}
    fact = facts.get(src.fact_id) if src.fact_id else None
    by_quote = fact is not None and src.quote is not None and 1 <= src.quote <= len(fact.evidence)
    by_outcome = src.outcome is not None and 1 <= src.outcome <= len(sender.outcomes)
    supported = by_quote or by_outcome
    reason = src.reason if supported else f"no valid source: {src.reason}"
    return ClaimCheck(
        claim=src.claim,
        supported=supported,
        fact_id=src.fact_id if by_quote else None,
        reason=reason,
    )


def feedback_for(checks: list[ClaimCheck]) -> str:
    bad = [c for c in checks if not c.supported]
    return "\n".join(
        f'Unsupported claim: "{c.claim}". Remove it, or reword it so a research quote or a '
        f"sender outcome states it exactly."
        for c in bad
    )


class VerifierAgent:
    def __init__(self, *, client: anthropic.AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def verify(self, email: Email, report: ResearchReport, sender: Sender) -> VerifyRun:
        outcomes = "\n".join(f"{i}. {o}" for i, o in enumerate(sender.outcomes, 1))
        agent = await run_agent_loop(
            name="verifier",
            client=self._client,
            model=self._model,
            system=SYSTEM_PROMPT,
            user_message=USER_TEMPLATE.format(
                facts=facts_block(report),
                outcomes=outcomes,
                subject=email.subject,
                body=email.body,
            ),
            tools=[],
            output_model=VerifierOutput,
            max_turns=MAX_TURNS,
            token_budget=TOKEN_BUDGET,
            refusal_fallback=False,  # Haiku: "default" fallback mode not documented for it
        )
        if agent.output is None:
            return VerifyRun(None, agent, error=f"verifier failed: {agent.stop_reason}")
        if not agent.output.claims:
            # Every email opens with a sourced company fact, so "no claims" means the
            # verifier didn't do its job; don't let that count as a pass.
            return VerifyRun(None, agent, error="verifier found no factual claims")
        checks = [resolve(c, report, sender) for c in agent.output.claims]
        return VerifyRun(Verification(claims=checks, feedback=feedback_for(checks)), agent)
