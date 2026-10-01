import pytest
from pydantic import ValidationError

from app.schemas import (
    ClaimCheck,
    Email,
    Fact,
    Lead,
    PainPoint,
    PainPoints,
    ResearchReport,
    Verification,
)


def make_fact(fid: str = "f1", **kw) -> Fact:
    data = {
        "id": fid,
        "claim": "Acme launched a usage-based pricing tier in 2026.",
        "category": "product",
        "source_url": "https://acme.com/blog/pricing",
        "evidence": "Today we're launching usage-based pricing.",
    } | kw
    return Fact.model_validate(data)


def make_report(*facts: Fact) -> ResearchReport:
    return ResearchReport(
        domain="acme.com",
        company_name="Acme",
        summary="Acme sells widgets.",
        facts=list(facts) or [make_fact()],
    )


# --- Lead ---


@pytest.mark.parametrize(
    "raw",
    ["acme.com", "ACME.com", "https://www.acme.com/about?x=1", "www.acme.com", " acme.com/ "],
)
def test_lead_domain_normalised(raw: str) -> None:
    assert Lead(domain=raw, person_name="Ana", person_role="CTO").domain == "acme.com"


@pytest.mark.parametrize("raw", ["", "acme", "not a domain"])
def test_lead_rejects_bad_domain(raw: str) -> None:
    with pytest.raises(ValidationError):
        Lead(domain=raw, person_name="Ana", person_role="CTO")


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Lead.model_validate(
            {"domain": "acme.com", "person_name": "A", "person_role": "B", "email": "x@y"}
        )


# --- Research ---


def test_fact_requires_valid_source_url() -> None:
    with pytest.raises(ValidationError):
        make_fact(source_url="not-a-url")
    with pytest.raises(ValidationError):
        make_fact(source_url=None)


def test_report_rejects_duplicate_fact_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        make_report(make_fact("f1"), make_fact("f1"))


# --- Pain points ---


def test_painpoints_sorted_and_top() -> None:
    pps = PainPoints(
        pain_points=[
            PainPoint(rank=2, title="B", rationale="r", supporting_fact_ids=["f1"]),
            PainPoint(rank=1, title="A", rationale="r", supporting_fact_ids=["f1"]),
        ]
    )
    assert pps.top.title == "A"


def test_painpoints_rank_gaps_rejected() -> None:
    with pytest.raises(ValidationError):
        PainPoints(
            pain_points=[PainPoint(rank=1, title="A", rationale="r", supporting_fact_ids=["f1"])]
            + [PainPoint(rank=3, title="C", rationale="r", supporting_fact_ids=["f1"])]
        )


def test_painpoints_unknown_fact_ids() -> None:
    pps = PainPoints(
        pain_points=[PainPoint(rank=1, title="A", rationale="r", supporting_fact_ids=["f1", "f9"])]
    )
    assert pps.unknown_fact_ids(make_report(make_fact("f1"))) == {"f9"}


# --- Email ---


def test_email_word_limit() -> None:
    ok = Email(subject="Hi", body="word " * 119, used_fact_ids=["f1"])
    assert ok.word_count == 119
    with pytest.raises(ValidationError, match="under 120"):
        Email(subject="Hi", body="word " * 120, used_fact_ids=["f1"])


# --- Verification ---


def test_verification_passed_is_derived() -> None:
    good = ClaimCheck(claim="c", supported=True, fact_id="f1", reason="matches f1")
    bad = ClaimCheck(claim="d", supported=False, reason="no source")
    assert Verification(claims=[good]).passed
    v = Verification(claims=[good, bad], feedback="remove claim d")
    assert not v.passed
    assert v.unsupported == [bad]


# --- Durability: dump -> reload must round-trip (runs resume from the DB) ---


@pytest.mark.parametrize(
    "obj",
    [
        Lead(domain="acme.com", person_name="Ana", person_role="CTO"),
        make_report(make_fact("f1"), make_fact("f2")),
        Email(subject="Hi", body="short body", used_fact_ids=["f1"]),
        Verification(claims=[ClaimCheck(claim="c", supported=True, reason="ok")]),
    ],
)
def test_json_round_trip(obj) -> None:
    assert type(obj).model_validate_json(obj.model_dump_json()) == obj
