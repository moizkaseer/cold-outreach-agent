"""Write -> lint -> verify -> revise, at most 2 revisions, then flag for a human.

Code controls the loop. Cheap checks run first: a draft that fails the code-checked rules
goes straight back to the writer without paying for a verifier call. No email is ever
sent from here; every result, verified or not, goes to human review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.agents.loop import AgentResult
from app.agents.verifier import VerifierAgent
from app.agents.writer import Revision, WriterAgent
from app.schemas import Email, Lead, PainPoint, ResearchReport, Sender, Verification

MAX_REVISIONS = 2  # CLAUDE.md


class DraftStatus(StrEnum):
    VERIFIED = "verified"  # passed lint + verifier; still needs human approval to send
    NEEDS_HUMAN = "needs_human"  # still has problems after max revisions (or verifier broke)
    FAILED = "failed"  # the writer produced no valid email at all


@dataclass
class DraftAttempt:
    email: Email | None
    problems: list[str]  # lint problems, or verifier feedback lines
    verification: Verification | None = None
    agents: list[AgentResult] = field(default_factory=list)  # for cost/tracing


@dataclass
class DraftResult:
    status: DraftStatus
    email: Email | None
    verification: Verification | None
    attempts: list[DraftAttempt]
    reason: str = ""

    @property
    def revisions(self) -> int:
        return max(0, len(self.attempts) - 1)


async def draft_email(
    *,
    writer: WriterAgent,
    verifier: VerifierAgent,
    lead: Lead,
    report: ResearchReport,
    pain_point: PainPoint,
    sender: Sender,
    max_revisions: int = MAX_REVISIONS,
) -> DraftResult:
    attempts: list[DraftAttempt] = []
    revision: Revision | None = None

    for n in range(max_revisions + 1):
        last = n == max_revisions
        w = await writer.write(lead, report, pain_point, sender, revision=revision)
        if w.email is None:
            attempts.append(DraftAttempt(None, [f"writer failed: {w.agent.stop_reason}"],
                                         agents=[w.agent]))  # fmt: skip
            return DraftResult(DraftStatus.FAILED, None, None, attempts, "writer failed")

        if w.problems:  # code-checked rules first; skip the paid verifier call
            attempts.append(DraftAttempt(w.email, w.problems, agents=[w.agent]))
            if last:
                return DraftResult(DraftStatus.NEEDS_HUMAN, w.email, None, attempts,
                                   "rule violations remain after max revisions")  # fmt: skip
            revision = Revision(n + 1, w.email, w.problems)
            continue

        v = await verifier.verify(w.email, report, sender)
        if v.verification is None:
            attempts.append(DraftAttempt(w.email, [v.error or "verifier failed"],
                                         agents=[w.agent, v.agent]))  # fmt: skip
            return DraftResult(DraftStatus.NEEDS_HUMAN, w.email, None, attempts,
                               v.error or "verifier failed")  # fmt: skip

        problems = v.verification.feedback.splitlines() if not v.verification.passed else []
        attempts.append(DraftAttempt(w.email, problems, v.verification, [w.agent, v.agent]))
        if v.verification.passed:
            return DraftResult(DraftStatus.VERIFIED, w.email, v.verification, attempts)
        if last:
            return DraftResult(DraftStatus.NEEDS_HUMAN, w.email, v.verification, attempts,
                               "unsupported claims remain after max revisions")  # fmt: skip
        revision = Revision(n + 1, w.email, problems)

    raise AssertionError("unreachable")  # the loop always returns on its last iteration
