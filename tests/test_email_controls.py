"""Planted-flaw controls for the email judge. Pure code, no API calls."""

import pytest

from app.schemas import Email
from evals.email_controls import (
    EXTRA_PAIN,
    FLAWS,
    HYPE,
    OFF_ROLE,
    PAIN_AS_FACT,
    VAGUE_OUTCOME,
    Case,
    Judged,
    apply_flaw,
    build_cases,
    summarize,
)
from evals.email_judge import CRITERIA
from tests.samples import LEAD, REPORT, SENDER
from tests.test_writer import GOOD_BODY, PAIN

ORIGINAL = Case("acme.com", LEAD, REPORT, PAIN,
                Email(subject="s", body=GOOD_BODY, used_fact_ids=["f1", "f2"]))  # fmt: skip
FLAW = {f.name: f for f in FLAWS}


def flawed(name: str) -> Case:
    c = apply_flaw(ORIGINAL, FLAW[name], SENDER)
    assert c is not None and c.flaw == name and c.target == FLAW[name].target
    return c


def test_every_flaw_targets_a_real_llm_criterion() -> None:
    assert {f.target for f in FLAWS} <= set(CRITERIA)


def test_generic_opener_replaces_only_the_first_sentence() -> None:
    body = flawed("generic_opener").email.body
    assert "Series C" not in body.split("\n\n")[1].split(".")[0]
    assert body.startswith("Hi Ana,\n\nI've been following Acme")
    assert "With 120 open roles" in body  # rest of the paragraph untouched


@pytest.mark.parametrize("name,text", [("extra_pain_point", EXTRA_PAIN),
                                       ("pain_as_fact", PAIN_AS_FACT)])  # fmt: skip
def test_inserted_pain_goes_just_before_the_outcome(name: str, text: str) -> None:
    paras = flawed(name).email.body.split("\n\n")
    i = paras.index(text)
    assert "30%" in paras[i + 1]


def test_changed_outcome_number_edits_the_outcome_only() -> None:
    body = flawed("changed_outcome_number").email.body
    assert "by 67% at Northwind" in body  # 30 -> 30*2+7
    assert "$60M" in body and "120 open roles" in body and "15-minute" in body


def test_outcome_found_even_when_a_company_number_collides() -> None:
    # A company number equal to an outcome number earlier in the email must not be edited.
    body = GOOD_BODY.replace("120 open roles", "30 open roles")
    o = Case("acme.com", LEAD, REPORT, PAIN, Email(subject="s", body=body, used_fact_ids=["f1"]))
    out = apply_flaw(o, FLAW["changed_outcome_number"], SENDER)
    assert out is not None
    assert "30 open roles" in out.email.body and "by 67%" in out.email.body


def test_vague_outcome_and_hype() -> None:
    v = flawed("vague_outcome").email.body
    assert VAGUE_OUTCOME in v and "30%" not in v
    paras = flawed("salesy_hype").email.body.split("\n\n")
    assert paras[paras.index(HYPE) + 1] == SENDER.ask


def test_wrong_role_changes_the_recipient_not_the_email() -> None:
    c = flawed("wrong_role")
    assert c.lead.person_role == OFF_ROLE and c.email == ORIGINAL.email
    assert ORIGINAL.lead.person_role != OFF_ROLE  # original untouched


def test_flaw_skipped_when_it_cannot_apply_or_breaks_word_limit() -> None:
    no_outcome = Case("acme.com", LEAD, REPORT, PAIN, Email(
        subject="s", used_fact_ids=["f1"],
        body="Hi Ana,\n\nCongrats on the Series C.\n\nOpen to a 15-minute call next week?\n\nSam",
    ))  # fmt: skip
    assert apply_flaw(no_outcome, FLAW["vague_outcome"], SENDER) is None

    long_body = GOOD_BODY.replace("Congrats", "Congrats " + "really " * 72)
    long = Case("acme.com", LEAD, REPORT, PAIN, Email(subject="s", body=long_body,
                                                      used_fact_ids=["f1"]))  # fmt: skip
    assert apply_flaw(long, FLAW["salesy_hype"], SENDER) is None  # would be 120+ words


def test_build_cases_keeps_originals_first() -> None:
    cases, skipped = build_cases([ORIGINAL], SENDER)
    assert cases[0] is ORIGINAL and len(cases) == 1 + len(FLAWS) and skipped == []


def verdicts(**fails: bool) -> dict[str, bool]:
    return {c: not fails.get(c, False) for c in CRITERIA}


def test_summarize_catch_pass_and_flip_rates() -> None:
    orig = Judged(ORIGINAL, runs=[verdicts(), verdicts(human_tone=True)])
    caught = Judged(flawed("generic_opener"), runs=[verdicts(specific_opener=True)] * 2)
    missed = Judged(flawed("salesy_hype"), runs=[verdicts(), verdicts(human_tone=True)])
    s = summarize([orig, caught, missed])

    assert s["originals"] == 1 and s["flawed_copies"] == 2
    assert s["original_pass_rate"]["human_tone"] == 0.5
    assert s["original_pass_rate"]["specific_opener"] == 1.0
    assert s["catch_rate"]["generic_opener"] == {"target": "specific_opener", "cases": 1,
                                                 "catch_rate": 1.0}  # fmt: skip
    assert s["catch_rate"]["salesy_hype"]["catch_rate"] == 0.5
    assert s["mean_catch_rate"] == 0.75
    # human_tone flipped on 2 of 3 emails; nothing else flipped.
    assert s["flip_rate"]["human_tone"] == pytest.approx(0.667, abs=1e-3)
    assert s["flip_rate"]["specific_opener"] == 0.0
    assert s["overall_flip_rate"] == pytest.approx(2 / (3 * len(CRITERIA)), abs=1e-3)


def test_summarize_single_run_has_no_flip_rate() -> None:
    s = summarize([Judged(ORIGINAL, runs=[verdicts()])])
    assert s["flip_rate"] is None and s["mean_catch_rate"] is None
