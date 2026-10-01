"""Verifier + revise loop tests. Scripted fakes only — no API calls."""

from typing import Any

from app.agents.drafting import DraftStatus, draft_email
from app.agents.loop import FINISH_TOOL_NAME
from app.agents.verifier import VerifierAgent
from app.agents.verifier.agent import ClaimSource, resolve
from app.agents.writer import WriterAgent
from app.schemas import Email
from tests.fakes import FakeClient, response, tool_use
from tests.samples import LEAD, REPORT, SENDER
from tests.test_writer import GOOD_BODY, PAIN


def claim(text: str, fact_id=None, quote=None, outcome=None) -> dict[str, Any]:
    return {"claim": text, "fact_id": fact_id, "quote": quote, "outcome": outcome, "reason": "r"}


def finish(payload: dict[str, Any], id: str = "t1"):
    return response(tool_use(FINISH_TOOL_NAME, payload, id=id))


GOOD_EMAIL = {"subject": "Your Series C hiring", "body": GOOD_BODY, "used_fact_ids": ["f1", "f2"]}
BAD_EMAIL = {"subject": "s", "body": "We use AI. " + GOOD_BODY, "used_fact_ids": ["f1"]}
ALL_SUPPORTED = {
    "claims": [
        claim("$60M Series C", "f1", 1),
        claim("120 open roles", "f2", 1),
        claim("cut time-to-productivity by 30% at Northwind", outcome=1),
    ]
}
ONE_UNSUPPORTED = {"claims": [claim("$60M Series C", "f1", 1), claim("raised in May 2026")]}


# --- resolve: code decides support ---


def src(**kw) -> ClaimSource:
    return ClaimSource.model_validate(claim("c") | kw)


def test_resolve_requires_a_real_source() -> None:
    assert resolve(src(fact_id="f1", quote=2), REPORT, SENDER).supported  # f1 has 2 quotes
    assert not resolve(src(fact_id="f1", quote=3), REPORT, SENDER).supported  # no quote 3
    assert not resolve(src(fact_id="f9", quote=1), REPORT, SENDER).supported  # no fact f9
    assert not resolve(src(fact_id="f1"), REPORT, SENDER).supported  # fact without quote
    assert resolve(src(outcome=1), REPORT, SENDER).supported
    assert not resolve(src(outcome=2), REPORT, SENDER).supported  # sender has 1 outcome
    assert not resolve(src(), REPORT, SENDER).supported


async def test_verifier_builds_verification_and_feedback() -> None:
    client = FakeClient(finish(ONE_UNSUPPORTED))
    v = await VerifierAgent(client=client, model="haiku").verify(  # type: ignore[arg-type]
        Email.model_validate(GOOD_EMAIL), REPORT, SENDER
    )
    assert v.verification is not None and not v.verification.passed
    assert 'Unsupported claim: "raised in May 2026"' in v.verification.feedback
    assert "fallbacks" not in client.requests[0]
    assert "<email>" in client.requests[0]["messages"][0]["content"]


async def test_verifier_with_no_claims_is_an_error_not_a_pass() -> None:
    client = FakeClient(finish({"claims": []}))
    v = await VerifierAgent(client=client, model="haiku").verify(  # type: ignore[arg-type]
        Email.model_validate(GOOD_EMAIL), REPORT, SENDER
    )
    assert v.verification is None and "no factual claims" in (v.error or "")


# --- draft_email: the revise loop ---


async def run_loop(writer_responses, verifier_responses):
    wc, vc = FakeClient(*writer_responses), FakeClient(*verifier_responses)
    result = await draft_email(
        writer=WriterAgent(client=wc, model="sonnet"),  # type: ignore[arg-type]
        verifier=VerifierAgent(client=vc, model="haiku"),  # type: ignore[arg-type]
        lead=LEAD, report=REPORT, pain_point=PAIN, sender=SENDER,
    )  # fmt: skip
    return result, wc, vc


async def test_verified_first_try() -> None:
    r, wc, vc = await run_loop([finish(GOOD_EMAIL)], [finish(ALL_SUPPORTED)])
    assert r.status is DraftStatus.VERIFIED and r.revisions == 0
    assert r.verification is not None and r.verification.passed
    assert len(wc.requests) == 1 and len(vc.requests) == 1


async def test_lint_failure_skips_verifier_then_revises() -> None:
    r, wc, vc = await run_loop(
        [finish(BAD_EMAIL), finish(GOOD_EMAIL, id="t2")], [finish(ALL_SUPPORTED)]
    )
    assert r.status is DraftStatus.VERIFIED and r.revisions == 1
    assert len(vc.requests) == 1  # verifier only called for the clean draft
    revision_prompt = wc.requests[1]["messages"][0]["content"]
    assert "Revision 1" in revision_prompt and "AI or automation" in revision_prompt


async def test_unsupported_claim_feedback_reaches_writer() -> None:
    r, wc, vc = await run_loop(
        [finish(GOOD_EMAIL), finish(GOOD_EMAIL, id="t2")],
        [finish(ONE_UNSUPPORTED), finish(ALL_SUPPORTED, id="v2")],
    )
    assert r.status is DraftStatus.VERIFIED and r.revisions == 1
    assert "raised in May 2026" in wc.requests[1]["messages"][0]["content"]


async def test_flags_for_human_after_two_revisions() -> None:
    r, wc, vc = await run_loop(
        [finish(GOOD_EMAIL, id=f"w{i}") for i in range(3)],
        [finish(ONE_UNSUPPORTED, id=f"v{i}") for i in range(3)],
    )
    assert r.status is DraftStatus.NEEDS_HUMAN
    assert len(wc.requests) == 3 and r.revisions == 2  # 1 draft + 2 revisions, no more
    assert r.email is not None  # last draft kept for the reviewer, with its problems
    assert any("May 2026" in p for p in r.attempts[-1].problems)


async def test_lint_never_fixed_flags_for_human_without_verifier() -> None:
    r, wc, vc = await run_loop([finish(BAD_EMAIL, id=f"w{i}") for i in range(3)], [])
    assert r.status is DraftStatus.NEEDS_HUMAN and r.verification is None
    assert vc.requests == []


async def test_verifier_breakdown_goes_to_human() -> None:
    r, _, _ = await run_loop([finish(GOOD_EMAIL)], [finish({"claims": []})])
    assert r.status is DraftStatus.NEEDS_HUMAN and "no factual claims" in r.reason


async def test_writer_failure() -> None:
    refused = response(tool_use(FINISH_TOOL_NAME, GOOD_EMAIL), stop="refusal")
    r, _, vc = await run_loop([refused], [])
    assert r.status is DraftStatus.FAILED and r.email is None and vc.requests == []
