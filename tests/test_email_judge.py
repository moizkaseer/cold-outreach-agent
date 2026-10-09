"""Email rubric judge tests. Scripted fakes only."""

import pytest

from app.agents.loop import FINISH_TOOL_NAME
from app.schemas import Email
from evals.email_judge import (
    CODE_CRITERIA,
    CRITERIA,
    NUMBERS_CRITERION,
    RULES_CRITERION,
    EmailJudgeError,
    company_numbers,
    judge_email,
)
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
    assert set(s.passed) == set(CRITERIA) | set(CODE_CRITERIA)


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
    assert s.score == pytest.approx(7 / 8)


async def test_duplicate_criterion_is_an_error() -> None:
    payload = results()
    payload["results"][-1] = {**payload["results"][0]}  # right length, one name twice
    with pytest.raises(EmailJudgeError, match="each criterion once"):
        await judge(payload)


async def test_empty_results_are_retried() -> None:
    # Seen for real with Haiku: an empty list. Schema rejects it, the loop retries once.
    client = FakeClient(
        response(tool_use(FINISH_TOOL_NAME, {"results": []})),
        response(tool_use(FINISH_TOOL_NAME, results())),
    )
    s = await judge_email(client=client, model="haiku", email=EMAIL, lead=LEAD,  # type: ignore[arg-type]
                          report=REPORT, pain_point=PAIN, sender=SENDER)  # fmt: skip
    assert s.score == 1.0 and len(client.requests) == 2


def test_company_numbers_ignore_the_senders_own_numbers() -> None:
    # GOOD_BODY: "$60M" and "120 open roles" are about the company; the 30% outcome and
    # the "15-minute" ask are the sender's own and don't count.
    assert company_numbers(EMAIL, SENDER) == ["60", "120"]


async def test_stat_pileup_fails_few_numbers() -> None:
    dump = Email(subject="s", used_fact_ids=["f1"], body=GOOD_BODY.replace(
        "Congrats on the $60M Series C.",
        "Congrats on the $60M Series C, 480% customer growth and 1,200% product adoption.",
    ))  # fmt: skip
    s = await judge(results(), email=dump)
    assert (
        s.passed[NUMBERS_CRITERION] is False and "4 company numbers" in s.reasons[NUMBERS_CRITERION]
    )
