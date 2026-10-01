"""Run the research agent on the golden set and score it.

    uv run python -m evals.run_evals                      # companies with verified facts
    uv run python -m evals.run_evals --include-unverified # dev mode: score vs drafted facts
    uv run python -m evals.run_evals --domains stripe.com linear.app --concurrency 2

Metrics (micro-averaged over all companies):
- grounding    kept facts / all facts the agent submitted   (code-checked)
- faithfulness claims the judge says match their quote / kept facts   (LLM judge)
- coverage     golden facts the agent found / golden facts            (LLM judge)
plus run health (finished / total), turns, latency and estimated cost.

Results are written to evals/results/<timestamp>.json, so runs can be compared over time.
Every run spends real money (research plus judge). A spending cap (--max-cost, default
$1.00) stops new companies from starting once estimated spend reaches it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Any

import anthropic

from app.agents.research import ResearchAgent, ResearchRun
from app.config import get_settings
from app.observability import setup_tracing
from app.pricing import PRICES, estimate_cost_usd
from app.schemas import Lead, ResearchReport
from app.spend import SpendTracker, run_capped
from evals.golden import GoldenCompany, load_golden_set
from evals.judge import Judgement, judge_report

RESULTS_DIR = Path(__file__).parent / "results"

# Synthetic recipient: evals measure company research, not people.
EVAL_PERSON = ("Alex Morgan", "Head of Growth")

RunResearch = Callable[[Lead], Awaitable[ResearchRun]]
Judge = Callable[[ResearchReport, list[str]], Awaitable[Judgement]]


@dataclass
class CompanyResult:
    domain: str
    finished: bool
    stop_reason: str
    error: str | None = None
    skipped: bool = False  # not run because the spending cap was reached
    kept: int = 0
    rejected: int = 0
    faithful: int = 0
    golden_total: int = 0
    golden_covered: int = 0
    turns: int = 0
    seconds: float = 0.0
    research_cost_usd: float | None = None
    judge_cost_usd: float | None = None
    details: dict[str, Any] = field(default_factory=dict)


def golden_claims(company: GoldenCompany, include_unverified: bool) -> list[str]:
    facts = company.facts if include_unverified else company.verified_facts
    return [f.claim for f in facts]


async def evaluate_company(
    company: GoldenCompany,
    *,
    run_research: RunResearch,
    judge: Judge,
    research_model: str,
    judge_model: str,
    include_unverified: bool,
) -> CompanyResult:
    golden = golden_claims(company, include_unverified)
    lead = Lead(domain=company.domain, person_name=EVAL_PERSON[0], person_role=EVAL_PERSON[1])

    t0 = time.perf_counter()
    try:
        run = await run_research(lead)
    except Exception as e:  # one broken company must not kill the whole eval
        return CompanyResult(
            company.domain,
            False,
            "exception",
            error=f"{type(e).__name__}: {e}",
            golden_total=len(golden),
            seconds=time.perf_counter() - t0,
        )
    seconds = time.perf_counter() - t0

    a = run.agent
    research_cost = estimate_cost_usd(research_model, a.usage)
    result = CompanyResult(
        domain=company.domain,
        finished=run.report is not None,
        stop_reason=a.stop_reason.value,
        rejected=len(run.rejected),
        golden_total=len(golden),
        turns=a.turns,
        seconds=round(seconds, 1),
        research_cost_usd=None if research_cost is None else research_cost + run.search_cost_usd,
        details={
            "tool_calls": [
                {"turn": c.turn, "tool": c.name, "input": c.input, "error": c.is_error}
                for c in a.tool_calls
            ],
            "rejected": [
                {
                    "id": r.fact.id,
                    "claim": r.fact.claim,
                    "url": str(r.fact.source_url),
                    "reason": r.reason,
                }
                for r in run.rejected
            ],
            "usage": asdict(a.usage),
        },
    )
    if run.report is None:
        return result

    report = run.report
    result.kept = len(report.facts)
    result.details["facts"] = [
        {"id": f.id, "claim": f.claim, "url": str(f.source_url), "evidence": f.evidence}
        for f in report.facts
    ]
    try:
        j = await judge(report, golden)
    except Exception as e:
        result.error = f"judge: {type(e).__name__}: {e}"
        return result

    result.faithful = sum(v.faithful for v in j.facts)
    result.golden_covered = sum(v.covered for v in j.golden)
    result.judge_cost_usd = estimate_cost_usd(judge_model, j.agent.usage) if j.agent else 0.0
    result.details["judge"] = {
        "facts": [asdict(v) for v in j.facts],
        "golden": [v.model_dump() for v in j.golden],
        "raw": j.raw.model_dump() if j.raw else None,
    }
    return result


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


async def run_companies(
    companies: list[GoldenCompany],
    evaluate_one: Callable[[GoldenCompany], Awaitable[CompanyResult]],
    *,
    tracker: SpendTracker,
    concurrency: int,
) -> list[CompanyResult]:
    """Evaluate companies with limited concurrency, stopping new starts at the spend cap."""
    return await run_capped(
        companies,
        evaluate_one,
        tracker=tracker,
        concurrency=concurrency,
        cost_of=lambda r: (r.research_cost_usd or 0.0) + (r.judge_cost_usd or 0.0),
        on_skip=lambda c: CompanyResult(c.domain, False, "skipped_budget", skipped=True),
    )


def summarize(results: list[CompanyResult]) -> dict[str, Any]:
    skipped = sum(r.skipped for r in results)
    results = [r for r in results if not r.skipped]  # skipped companies aren't scored
    finished = [r for r in results if r.finished]
    costs = [r.research_cost_usd for r in results if r.research_cost_usd is not None]
    judge_costs = [r.judge_cost_usd for r in results if r.judge_cost_usd is not None]
    return {
        "companies": len(results),
        "skipped_budget": skipped,
        "finished": len(finished),
        "errors": sum(r.error is not None for r in results),
        "grounding": _ratio(
            sum(r.kept for r in results), sum(r.kept + r.rejected for r in results)
        ),
        "faithfulness": _ratio(sum(r.faithful for r in finished), sum(r.kept for r in finished)),
        "coverage": _ratio(
            sum(r.golden_covered for r in results), sum(r.golden_total for r in results)
        ),
        "avg_kept_facts": round(mean(r.kept for r in finished), 1) if finished else None,
        "avg_turns": round(mean(r.turns for r in results), 1) if results else None,
        "median_seconds": round(median(r.seconds for r in results), 1) if results else None,
        "avg_research_cost_usd": round(mean(costs), 4) if costs else None,
        "total_cost_usd": round(sum(costs) + sum(judge_costs), 4) if costs else None,
    }


def _git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


async def main(args: argparse.Namespace) -> None:
    s = get_settings()
    setup_tracing("outreach-evals", s.logfire_token)
    judge_model = args.judge_model or s.model_fast
    companies = load_golden_set()
    if args.domains:
        companies = [c for c in companies if c.domain in set(args.domains)]
    companies = [c for c in companies if golden_claims(c, args.include_unverified)]
    if args.limit:
        companies = companies[: args.limit]
    if not companies:
        raise SystemExit(
            "No companies to evaluate. Verify some golden facts (see "
            "evals/golden_set/README.md) or pass --include-unverified."
        )

    tracker = SpendTracker(args.max_cost if args.max_cost > 0 else None)
    if tracker.limit_usd is not None and not {s.model_smart, judge_model} <= PRICES.keys():
        # With unknown prices every run would count as $0 and the cap could never trigger.
        raise SystemExit("--max-cost needs known prices for both models (see app/pricing.py)")

    researcher = ResearchAgent.from_settings(s)
    judge_client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())

    async def judge(report: ResearchReport, golden: list[str]) -> Judgement:
        return await judge_report(
            client=judge_client, model=judge_model, report=report, golden_claims=golden
        )

    async def evaluate_one(c: GoldenCompany) -> CompanyResult:
        r = await evaluate_company(
            c,
            run_research=researcher.run,
            judge=judge,
            research_model=s.model_smart,
            judge_model=judge_model,
            include_unverified=args.include_unverified,
        )
        print(
            f"{r.domain:<18} {r.stop_reason:<14} kept={r.kept:<2} rejected={r.rejected:<2} "
            f"faithful={r.faithful:<2} covered={r.golden_covered}/{r.golden_total} "
            f"{r.seconds:>5.0f}s{'  ERROR ' + r.error if r.error else ''}",
            flush=True,
        )
        return r

    results = await run_companies(
        companies, evaluate_one, tracker=tracker, concurrency=args.concurrency
    )
    summary = summarize(list(results))

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = RESULTS_DIR / f"{stamp}.json"
    out.write_text(
        json.dumps(
            {
                "config": {
                    "git_commit": _git_commit(),
                    "research_model": s.model_smart,
                    "judge_model": judge_model,
                    "max_turns": s.research_max_turns,
                    "token_budget": s.research_max_tokens,
                    "include_unverified": args.include_unverified,
                    "max_cost_usd": tracker.limit_usd,
                    "estimated_spend_usd": round(tracker.spent_usd, 4),
                },
                "summary": summary,
                "results": [asdict(r) for r in results],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    print("\n" + json.dumps(summary, indent=2))
    print(
        f"\nestimated spend ${tracker.spent_usd:.4f}"
        + (f" of ${tracker.limit_usd:.2f} cap" if tracker.limit_usd else "")
    )
    print(f"wrote {out}")
    if args.include_unverified:
        print("NOTE: scored against UNVERIFIED golden facts; coverage is indicative only.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--domains", nargs="*")
    p.add_argument("--limit", type=int)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument(
        "--max-cost",
        type=float,
        default=1.0,
        help="USD cap on estimated spend; no new companies start once reached (0 = no cap)",
    )
    p.add_argument("--include-unverified", action="store_true")
    p.add_argument("--judge-model", help="defaults to MODEL_FAST from config")
    asyncio.run(main(p.parse_args()))
