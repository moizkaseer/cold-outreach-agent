"""Eval scoring tests: fake research runs and a scripted judge. No API calls."""

from typing import Any

import pytest

from app.agents.loop import FINISH_TOOL_NAME, AgentResult, StopReason, Usage
from app.agents.research.agent import RejectedFact, ResearchRun
from app.pricing import estimate_cost_usd
from app.schemas import Fact, Lead, ResearchReport
from evals.golden import GoldenCompany
from evals.judge import (
    FactVerdict,
    GoldenVerdict,
    JudgeError,
    Judgement,
    JudgeOutput,
    judge_report,
)
from evals.run_evals import CompanyResult, evaluate_company, summarize
from tests.fakes import FakeClient, response, tool_use


def fact(fid: str) -> Fact:
    return Fact(
        id=fid,
        claim=f"claim {fid}",
        category="customers",
        source_url="https://acme.com/about",
        evidence=f"quote {fid}",
    )


def report(*ids: str) -> ResearchReport:
    return ResearchReport(
        domain="acme.com", company_name="Acme", summary="s", facts=[fact(i) for i in ids]
    )


COMPANY = GoldenCompany.model_validate(
    {
        "domain": "acme.com",
        "company_name": "Acme",
        "segment": "saas",
        "size": "startup",
        "facts": [
            {
                "claim": "g verified",
                "category": "customers",
                "source_url": "https://acme.com/about",
                "evidence": "x",
                "verified": True,
                "verified_on": "2026-10-02",
            },
            {
                "claim": "g unverified",
                "category": "customers",
                "source_url": "https://acme.com/about",
                "evidence": "y",
            },
        ],
    }
)


# --- pricing ---


def test_cost_estimate() -> None:
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=1_000_000)
    # 1M in * $2 + 1M cache read * $0.20 + 100k out * $10/M
    assert estimate_cost_usd("claude-sonnet-5-5", u) == pytest.approx(2.0 + 0.2 + 1.0)
    assert estimate_cost_usd("unknown-model", u) is None


# --- judge ---


def verdicts(fact_ids: list[str], golden_ids: list[str]) -> dict[str, Any]:
    return {
        "facts": [{"fact_id": i, "faithful": True, "reason": "ok"} for i in fact_ids],
        "golden": [
            {"golden_id": g, "covered": g == "g1", "matched_fact_ids": [], "reason": "r"}
            for g in golden_ids
        ],
    }


async def test_judge_ok_and_no_fallback() -> None:
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, verdicts(["f1"], ["g1", "g2"]))))
    j = await judge_report(
        client=client,
        model="haiku",
        report=report("f1"),  # type: ignore[arg-type]
        golden_claims=["a", "b"],
    )
    assert [v.covered for v in j.output.golden] == [True, False]
    assert "fallbacks" not in client.requests[0]
    assert "quote f1" in client.requests[0]["messages"][0]["content"]


async def test_judge_must_cover_every_id() -> None:
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, verdicts(["f1"], ["g1"]))))
    with pytest.raises(JudgeError, match="golden"):
        await judge_report(
            client=client,
            model="haiku",
            report=report("f1"),  # type: ignore[arg-type]
            golden_claims=["a", "b"],
        )


async def test_judge_skips_call_when_no_facts() -> None:
    client = FakeClient()  # any call would fail: no scripted responses
    j = await judge_report(
        client=client,
        model="haiku",
        report=report(),  # type: ignore[arg-type]
        golden_claims=["a"],
    )
    assert j.agent is None and not j.output.golden[0].covered and client.requests == []


# --- evaluate_company ---


def run_with(rep: ResearchReport | None, rejected: int = 0) -> ResearchRun:
    agent: AgentResult[ResearchReport] = AgentResult(
        output=rep,
        stop_reason=StopReason.FINISHED if rep else StopReason.MAX_TURNS,
        turns=4,
        usage=Usage(input_tokens=10_000, output_tokens=1_000),
    )
    rej = [
        RejectedFact(fact(f"f{90 + i}"), "evidence quote not found on the fetched page")
        for i in range(rejected)
    ]
    return ResearchRun(rep, agent, rej, search_cost_usd=0.01)


async def run_eval(run: ResearchRun, include_unverified: bool = False, judge=None) -> CompanyResult:
    async def research(lead: Lead) -> ResearchRun:
        assert lead.domain == "acme.com"
        return run

    async def fake_judge(rep: ResearchReport, golden: list[str]) -> Judgement:
        out = JudgeOutput(
            facts=[FactVerdict(fact_id=f.id, faithful=f.id != "f2", reason="r") for f in rep.facts],
            golden=[
                GoldenVerdict(golden_id=f"g{i}", covered=True, reason="r")
                for i, _ in enumerate(golden, 1)
            ],
        )
        return Judgement(out, None)

    return await evaluate_company(
        COMPANY,
        run_research=research,
        judge=judge or fake_judge,
        research_model="claude-sonnet-5-5",
        judge_model="claude-haiku-4-5",
        include_unverified=include_unverified,
    )


async def test_evaluate_company_scores() -> None:
    r = await run_eval(run_with(report("f1", "f2", "f3"), rejected=1))
    assert (r.kept, r.rejected, r.faithful) == (3, 1, 2)
    assert (r.golden_covered, r.golden_total) == (1, 1)  # verified facts only by default
    # 10k in * $2/M + 1k out * $10/M + $0.01 search
    assert r.research_cost_usd == pytest.approx(0.02 + 0.01 + 0.01)
    assert r.details["rejected"][0]["reason"].startswith("evidence")


async def test_include_unverified_widens_golden() -> None:
    r = await run_eval(run_with(report("f1")), include_unverified=True)
    assert r.golden_total == 2


async def test_unfinished_run_is_not_judged() -> None:
    async def judge_must_not_run(rep, golden):
        raise AssertionError("judge called on a failed run")

    r = await run_eval(run_with(None), judge=judge_must_not_run)
    assert not r.finished and r.stop_reason == "max_turns" and r.kept == 0


async def test_research_exception_is_recorded() -> None:
    async def boom(lead: Lead) -> ResearchRun:
        raise RuntimeError("exa down")

    r = await evaluate_company(
        COMPANY,
        run_research=boom,
        judge=None,  # type: ignore[arg-type]
        research_model="m",
        judge_model="m",
        include_unverified=False,
    )
    assert r.error == "RuntimeError: exa down" and not r.finished


# --- summary ---


def test_summarize_micro_averages() -> None:
    a = CompanyResult(
        "a.com",
        True,
        "finished",
        kept=4,
        rejected=1,
        faithful=3,
        golden_total=3,
        golden_covered=2,
        turns=5,
        seconds=40,
        research_cost_usd=0.1,
        judge_cost_usd=0.01,
    )
    b = CompanyResult(
        "b.com",
        False,
        "max_turns",
        rejected=0,
        golden_total=2,
        turns=10,
        seconds=80,
        research_cost_usd=0.3,
    )
    s = summarize([a, b])
    assert s["grounding"] == 0.8  # 4 / 5
    assert s["faithfulness"] == 0.75  # 3 / 4, finished runs only
    assert s["coverage"] == 0.4  # 2 / 5: a failed run still counts against coverage
    assert s["finished"] == 1 and s["total_cost_usd"] == 0.41


def test_summarize_empty_denominators() -> None:
    s = summarize([CompanyResult("a.com", False, "refused")])
    assert s["grounding"] is None and s["faithfulness"] is None and s["coverage"] is None
