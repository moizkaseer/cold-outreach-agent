import pytest

from app.spend import SpendTracker
from evals.golden import GoldenCompany
from evals.run_evals import CompanyResult, run_companies, summarize


def test_tracker() -> None:
    t = SpendTracker(limit_usd=0.10)
    t.add(0.04)
    t.add(None)  # unknown cost adds nothing
    assert not t.exhausted and t.remaining_usd == pytest.approx(0.06)
    t.add(0.07)
    assert t.exhausted and t.remaining_usd == 0.0
    assert not SpendTracker(limit_usd=None, spent_usd=999).exhausted


def company(domain: str) -> GoldenCompany:
    return GoldenCompany.model_validate(
        {
            "domain": domain, "company_name": "X", "segment": "saas", "size": "startup",
            "facts": [{"claim": "c", "category": "other", "source_url": f"https://{domain}/",
                       "evidence": "e"}],
        }
    )  # fmt: skip


async def test_cap_stops_new_companies() -> None:
    started: list[str] = []

    async def evaluate_one(c: GoldenCompany) -> CompanyResult:
        started.append(c.domain)
        return CompanyResult(
            c.domain, True, "finished", research_cost_usd=0.04, judge_cost_usd=0.01
        )

    companies = [company(f"c{i}.com") for i in range(5)]
    tracker = SpendTracker(limit_usd=0.10)
    results = await run_companies(companies, evaluate_one, tracker=tracker, concurrency=1)

    # 0.05 per company: after 2 companies spend hits 0.10, so the rest are skipped.
    assert started == ["c0.com", "c1.com"]
    assert [r.skipped for r in results] == [False, False, True, True, True]
    assert tracker.spent_usd == pytest.approx(0.10)

    s = summarize(results)
    assert s["companies"] == 2 and s["skipped_budget"] == 3  # skipped aren't scored


async def test_no_cap_runs_everything() -> None:
    async def evaluate_one(c: GoldenCompany) -> CompanyResult:
        return CompanyResult(c.domain, True, "finished", research_cost_usd=1.0)

    results = await run_companies(
        [company("a.com"), company("b.com")], evaluate_one,
        tracker=SpendTracker(limit_usd=None), concurrency=2,
    )  # fmt: skip
    assert not any(r.skipped for r in results)
