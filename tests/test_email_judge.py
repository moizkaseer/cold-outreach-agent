"""Email rubric judge tests. Scripted fakes only."""

import pytest

from app.agents.loop import FINISH_TOOL_NAME
from app.schemas import Email
from evals.email_judge import CRITERIA, RULES_CRITERION, EmailJudgeError, judge_email
from tests.fakes import FakeClient, response, tool_use
from tests.samples import LEAD, REPORT, SENDER
from tests.test_writer import GOOD_BODY, PAIN

EMAIL = Email(subject="Your Series C hiring", body=GOOD_BODY, used_fact_ids=["f1", "f2"])
EVIDENCE = {
    "specific_opener": "Congrats on the $60M Series C.",
    "single_pain_point": "getting new hires productive quickly is often the hard part",
    "pain_as_hypothesis": "is often the hard part",
    "one_sender_outcome": "we cut time-to-productivity for new hires by 30% at Northwind",
}


def results(overrides: dict | None = None, drop: str | None = None) -> dict:
    rows = []
    for name in CRITERIA:
        if name == drop:
            continue
        row = {"criterion": name, "passed": True, "evidence": EVIDENCE.get(name, ""), "reason": "r"}
        row.update((overrides or {}).get(name, {}))
        rows.append(row)
    return {"results": rows}


async def judge(payload: dict, email: Email = EMAIL):
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, payload)))
    return await judge_email(
        client=client,
        model="haiku",
        email=email,
        lead=LEAD,  # type: ignore[arg-type]
        report=REPORT,
        pain_point=PAIN,
        sender=SENDER,
    )


async def test_all_pass() -> None:
    s = await judge(results())
    assert all(s.passed.values()) and s.score == 1.0
    assert set(s.passed) == set(CRITERIA) | {RULES_CRITERION}


async def test_pass_with_fake_evidence_is_rejected() -> None:
    s = await judge(results({"specific_opener": {"evidence": "Congrats on your IPO!"}}))
    assert s.passed["specific_opener"] is False
    assert "evidence not found" in s.reasons["specific_opener"]


async def test_criteria_without_evidence_requirement() -> None:
    s = await judge(results({"human_tone": {"evidence": ""}}))  # no quote needed
    assert s.passed["human_tone"] is True


async def test_code_rules_folded_in() -> None:
    bad = Email(subject="s", body="Our AI helps. " + GOOD_BODY, used_fact_ids=["f1"])
    s = await judge(results(), email=bad)
    assert s.passed[RULES_CRITERION] is False and "AI or automation" in s.reasons[RULES_CRITERION]
    assert s.score == pytest.approx(6 / 7)


async def test_missing_criterion_is_an_error() -> None:
    with pytest.raises(EmailJudgeError, match="each criterion once"):
        await judge(results(drop="human_tone"))
