"""Shared sample inputs for downstream-agent tests (pain points, writer, verifier)."""

from app.schemas import Fact, Lead, ResearchReport, Sender

LEAD = Lead(domain="acme.com", person_name="Ana Silva", person_role="Head of People")

REPORT = ResearchReport(
    domain="acme.com",
    company_name="Acme",
    summary="Acme sells payroll software and just raised a Series C.",
    facts=[
        Fact(
            id="f1",
            claim="Acme raised a $60M Series C in March 2026.",
            category="funding",
            source_url="https://acme.com/blog/series-c",
            evidence=["Today we announced our $60M Series C", "March 3, 2026"],
        ),
        Fact(
            id="f2",
            claim="Acme has 120 open roles.",
            category="hiring",
            source_url="https://acme.com/careers",
            evidence=["We're hiring for 120 open roles"],
        ),
    ],
)

SENDER = Sender(
    name="Sam Lee",
    company="Rampup",
    offer="We run onboarding programs for fast-growing teams.",
    outcomes=["cut time-to-productivity for new hires by 30% at Northwind"],
    ask="Open to a 15-minute call next week?",
)
