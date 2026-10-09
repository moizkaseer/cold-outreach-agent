"""Email rubric judge tests. Scripted fakes only."""

import pytest

from app.agents.loop import FINISH_TOOL_NAME
from app.schemas import Email
from evals.email_judge import (
    CODE_CRITERIA,
    CRITERIA,
    HUMAN_TONE,
    JUDGED_CRITERIA,
    NUMBERS_CRITERION,
    PAIN_HYPOTHESIS,
    RULES_CRITERION,
    SINGLE_PAIN,
    EmailJudgeError,
    company_numbers,
    judge_email,
    opening_sentence,
)
from tests.fakes import FakeClient, response, tool_use
from tests.samples import LEAD, REPORT, SENDER
from tests.test_writer import GOOD_BODY, PAIN

EMAIL = Email(subject="Your Series C hiring", body=GOOD_BODY, used_fact_ids=["f1", "f2"])
EVIDENCE = {
    "specific_opener": "Congrats on the $60M Series C.",
    "one_sender_outcome": "we cut time-to-productivity for new hires by 30% at Northwind",
}
PAIN_QUOTE = "getting new hires productive quickly is often the hard part"
GOOD_PROBLEMS = [{"quote": PAIN_QUOTE, "is_given_pain_point": True, "framing": "hypothesis"}]


def results(
    overrides: dict | None = None,
    problems: list[dict] | None = None,
    tone: list[dict] | None = None,
    drop: str | None = None,
) -> dict:
    rows = []
    for name in JUDGED_CRITERIA:
        if name == drop:
            continue
        row = {"criterion": name, "passed": True, "evidence": EVIDENCE.get(name, ""), "reason": "r"}
        row.update((overrides or {}).get(name, {}))
        rows.append(row)
    return {
        "problems": GOOD_PROBLEMS if problems is None else problems,
        "tone_issues": tone or [],
        "pain_owner_roles": ["Head of People"],
        "results": rows,
    }


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
    s = await judge(results({"relevant_to_role": {"evidence": ""}}))  # no quote needed
    assert s.passed["relevant_to_role"] is True


async def test_code_rules_folded_in() -> None:
    bad = Email(subject="s", body=GOOD_BODY + " Our AI helps.", used_fact_ids=["f1"])
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


def test_opening_sentence_skips_greeting() -> None:
    assert opening_sentence(GOOD_BODY) == "Congrats on the $60M Series C."
    assert opening_sentence("No greeting here. Second.") == "No greeting here."


async def test_opener_evidence_must_come_from_the_opening_sentence() -> None:
    # Seen for real: a generic first sentence passed because sentence 2 was specific.
    s = await judge(results({"specific_opener": {"evidence": "With 120 open roles"}}))
    assert s.passed["specific_opener"] is False
    assert "opening sentence" in s.reasons["specific_opener"]


async def test_off_topic_problem_fails_single_pain_point() -> None:
    extra = {"quote": "retaining engineers is harder", "is_given_pain_point": False,
             "framing": "hypothesis"}  # fmt: skip
    s = await judge(results(problems=[*GOOD_PROBLEMS, extra]))
    assert s.passed[SINGLE_PAIN] is False and "retaining engineers" in s.reasons[SINGLE_PAIN]
    assert s.passed[PAIN_HYPOTHESIS] is True


async def test_asserted_problem_fails_hypothesis_even_with_a_sloppy_quote() -> None:
    # Fail-safe: a flagged problem counts even if its quote isn't verbatim.
    bad = {"quote": "not in the email", "is_given_pain_point": True, "framing": "asserted"}
    s = await judge(results(problems=[*GOOD_PROBLEMS, bad]))
    assert s.passed[PAIN_HYPOTHESIS] is False and s.passed[SINGLE_PAIN] is True


async def test_pain_passes_need_a_verifiable_quote() -> None:
    fake = [{"quote": "invented text", "is_given_pain_point": True, "framing": "hypothesis"}]
    s = await judge(results(problems=fake))
    assert s.passed[SINGLE_PAIN] is False and s.passed[PAIN_HYPOTHESIS] is False
    s = await judge(results(problems=[]))
    assert s.passed[SINGLE_PAIN] is False and s.passed[PAIN_HYPOTHESIS] is False


async def test_pain_criteria_are_not_model_verdicts() -> None:
    for c in (SINGLE_PAIN, PAIN_HYPOTHESIS, HUMAN_TONE):
        assert c not in JUDGED_CRITERIA and c in CRITERIA


async def test_verified_tone_issue_fails_human_tone() -> None:
    hype = GOOD_BODY.replace("Open to", "We're the industry-leading platform. Open to")
    e = Email(subject="s", body=hype, used_fact_ids=["f1"])
    s = await judge(results(tone=[{"quote": "industry-leading", "kind": "superlative"}]), email=e)
    assert s.passed[HUMAN_TONE] is False and "industry-leading" in s.reasons[HUMAN_TONE]


async def test_unverifiable_tone_complaint_is_ignored() -> None:
    # Noise was the problem: a complaint about words that aren't there doesn't count.
    s = await judge(results(tone=[{"quote": "synergy", "kind": "buzzword"}]))
    assert s.passed[HUMAN_TONE] is True


async def test_senders_own_wording_is_not_the_writers_tone_defect() -> None:
    # "cut time-to-productivity ... by 30%" comes verbatim from SENDER.outcomes.
    issue = {"quote": "cut time-to-productivity for new hires by 30%", "kind": "hype"}
    s = await judge(results(tone=[issue]))
    assert s.passed[HUMAN_TONE] is True


async def test_exclamation_mark_is_checked_by_code() -> None:
    e = Email(subject="s", body=GOOD_BODY.replace("Series C.", "Series C!"), used_fact_ids=["f1"])
    s = await judge(results({"specific_opener": {"evidence": "Congrats on the $60M Series C!"}}),
                    email=e)  # fmt: skip
    assert s.passed[HUMAN_TONE] is False and "exclamation" in s.reasons[HUMAN_TONE]
