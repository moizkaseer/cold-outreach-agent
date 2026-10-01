"""End-to-end email eval wiring, with every stage faked. No API calls."""

from app.agents.drafting import DraftAttempt, DraftResult, DraftStatus
from app.agents.loop import AgentResult, StopReason, Usage
from app.agents.painpoints import PainPointRun
from app.agents.research import ResearchRun
from app.schemas import Email, PainPoints, Sender
from evals.email_judge import CRITERIA, RULES_CRITERION, EmailScore
from evals.golden import load_golden_set
from evals.run_email_evals import evaluate_email_company, summarize_emails
from tests.samples import REPORT
from tests.test_writer import GOOD_BODY, PAIN

COMPANY = load_golden_set()[0]
SONNET, HAIKU = "claude-sonnet-5-5", "claude-haiku-4-5"


def agent(model: str, tokens: int = 100_000) -> AgentResult:
    return AgentResult(None, StopReason.FINISHED, 1, Usage(input_tokens=tokens), model=model)


EMAIL = Email(subject="s", body=GOOD_BODY, used_fact_ids=["f1"])


async def fake_research(lead):
    return ResearchRun(REPORT, agent(SONNET), search_cost_usd=0.01)


async def fake_pp(lead, report):
    return PainPointRun(PainPoints(pain_points=[PAIN]), agent(SONNET))


async def fake_draft(lead, report, pp):
    return DraftResult(
        DraftStatus.VERIFIED,
        EMAIL,
        None,
        [DraftAttempt(EMAIL, [], agents=[agent(SONNET), agent(HAIKU)])],
    )


async def fake_judge(email, lead, report, pp):
    passed = {c: True for c in CRITERIA} | {RULES_CRITERION: True, "human_tone": False}
    return EmailScore(passed, agent=agent(HAIKU))


async def run(**overrides):
    stages = {
        "research": fake_research,
        "pain_points": fake_pp,
        "draft": fake_draft,
        "judge": fake_judge,
    } | overrides
    return await evaluate_email_company(COMPANY, **stages)


async def test_happy_path_scores_and_costs() -> None:
    r = await run()
    assert r.status == "verified" and r.revisions == 0
    assert r.score == 6 / 7 and r.rubric["human_tone"] is False
    # 100k input tokens each: 3 Sonnet runs * $0.20 + 2 Haiku runs * $0.10 + $0.01 search
    assert r.cost_usd == 0.81
    assert r.details["email"]["subject"] == "s"


async def test_no_facts_stops_early() -> None:
    async def empty(lead):
        return ResearchRun(REPORT.model_copy(update={"facts": []}), agent(SONNET))

    async def must_not_run(*a):
        raise AssertionError("later stages must not run")

    r = await run(research=empty, pain_points=must_not_run)
    assert r.status == "no_facts" and r.cost_usd == 0.2


async def test_exception_is_recorded_with_cost_so_far() -> None:
    async def boom(lead, report, pp):
        raise RuntimeError("writer exploded")

    r = await run(draft=boom)
    assert r.status == "exception" and "writer exploded" in (r.error or "")
    assert r.cost_usd == 0.41  # research + pain points + search were still spent


def test_summary() -> None:
    from evals.run_email_evals import EmailEvalResult

    rs = [
        EmailEvalResult(
            "a.com",
            "verified",
            revisions=0,
            rubric={c: True for c in [*CRITERIA, RULES_CRITERION]},
            cost_usd=0.5,
        ),
        EmailEvalResult(
            "b.com",
            "needs_human",
            revisions=2,
            rubric={c: False for c in [*CRITERIA, RULES_CRITERION]},
            cost_usd=0.7,
        ),
        EmailEvalResult("c.com", "skipped_budget", skipped=True),
    ]
    s = summarize_emails(rs)
    assert s["companies"] == 2 and s["skipped_budget"] == 1
    assert s["verified_rate"] == 0.5 and s["avg_revisions"] == 1.0
    assert s["rubric_pass_rate"]["human_tone"] == 0.5 and s["mean_rubric_score"] == 0.5
    assert s["total_cost_usd"] == 1.2


def test_placeholder_sender_detected() -> None:
    from pathlib import Path

    from app.schemas import load_sender

    assert load_sender(Path("sender.example.json")).is_placeholder
    real = Sender(name="Moiz", company="X", offer="o", outcomes=["cut churn 10% at Y"])
    assert not real.is_placeholder
