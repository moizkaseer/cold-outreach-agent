"""End-to-end email eval: research -> pain points -> write/verify/revise -> rubric.

    uv run python -m evals.run_email_evals --domains stripe.com --max-cost 0.30
    uv run python -m evals.run_email_evals --limit 5 --sender sender.json

Needs a real sender profile (the example one is refused) and both API keys. Every run
spends real money; --max-cost (default $1.00) stops new companies once reached.

Reported per run: final status (verified / needs_human / failed), revisions used, rubric
pass rate per criterion, and estimated cost across every model involved.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

import anthropic

from app.agents.drafting import DraftResult, draft_email
from app.agents.painpoints import PainPointAgent, PainPointRun
from app.agents.research import ResearchAgent, ResearchRun
from app.agents.verifier import VerifierAgent
from app.agents.writer import WriterAgent
from app.config import get_settings
from app.observability import setup_tracing
from app.pricing import PRICES, total_cost_usd
from app.schemas import Email, Lead, PainPoint, ResearchReport, Sender, load_sender
from app.spend import SpendTracker, run_capped
from evals.email_judge import CODE_CRITERIA, CRITERIA, EmailScore, judge_email
from evals.golden import GoldenCompany, load_golden_set
from evals.run_evals import EVAL_PERSON, RESULTS_DIR, _git_commit

RunResearch = Callable[[Lead], Awaitable[ResearchRun]]
RunPainPoints = Callable[[Lead, ResearchReport], Awaitable[PainPointRun]]
RunDraft = Callable[[Lead, ResearchReport, PainPoint], Awaitable[DraftResult]]
RunJudge = Callable[[Email, Lead, ResearchReport, PainPoint], Awaitable[EmailScore]]


@dataclass
class EmailEvalResult:
    domain: str
    status: str  # verified | needs_human | failed | no_facts | no_pain_points | exception | ...
    skipped: bool = False
    error: str | None = None
    revisions: int = 0
    rubric: dict[str, bool] = field(default_factory=dict)
    cost_usd: float | None = None
    seconds: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def score(self) -> float | None:
        return sum(self.rubric.values()) / len(self.rubric) if self.rubric else None


async def evaluate_email_company(
    company: GoldenCompany,
    *,
    research: RunResearch,
    pain_points: RunPainPoints,
    draft: RunDraft,
    judge: RunJudge,
) -> EmailEvalResult:
    lead = Lead(domain=company.domain, person_name=EVAL_PERSON[0], person_role=EVAL_PERSON[1])
    agents: list = []  # every AgentResult, for cost
    search_cost = 0.0
    t0 = time.perf_counter()

    def finish(status: str, **kw: Any) -> EmailEvalResult:
        cost = total_cost_usd(agents)
        return EmailEvalResult(
            company.domain,
            status,
            cost_usd=None if cost is None else round(cost + search_cost, 4),
            seconds=round(time.perf_counter() - t0, 1),
            **kw,
        )

    try:
        rr = await research(lead)
        agents.append(rr.agent)
        search_cost = rr.search_cost_usd
        if rr.report is None or not rr.report.facts:
            return finish("no_facts")
        report = rr.report

        pr = await pain_points(lead, report)
        agents.append(pr.agent)
        if pr.pain_points is None:
            return finish("no_pain_points")
        top = pr.pain_points.top

        d = await draft(lead, report, top)
        agents.extend(a for att in d.attempts for a in att.agents)
        details: dict[str, Any] = {
            "pain_point": top.model_dump(),
            "email": d.email.model_dump() if d.email else None,
            "reason": d.reason,
            "last_problems": d.attempts[-1].problems if d.attempts else [],
        }
        if d.email is None:
            return finish(d.status.value, revisions=d.revisions, details=details)

        score = await judge(d.email, lead, report, top)
        if score.agent is not None:
            agents.append(score.agent)
        details["rubric_reasons"] = score.reasons
        return finish(d.status.value, revisions=d.revisions, rubric=score.passed,
                      details=details)  # fmt: skip
    except Exception as e:  # one broken company must not kill the eval
        return finish("exception", error=f"{type(e).__name__}: {e}")


def summarize_emails(results: list[EmailEvalResult]) -> dict[str, Any]:
    skipped = sum(r.skipped for r in results)
    results = [r for r in results if not r.skipped]
    judged = [r for r in results if r.rubric]
    drafted = [r for r in results if r.status in ("verified", "needs_human")]
    costs = [r.cost_usd for r in results if r.cost_usd is not None]
    criteria = [*CRITERIA, *CODE_CRITERIA]
    return {
        "companies": len(results),
        "skipped_budget": skipped,
        "status": dict(Counter(r.status for r in results)),
        "verified_rate": round(sum(r.status == "verified" for r in results) / len(results), 3)
        if results
        else None,
        "avg_revisions": round(mean(r.revisions for r in drafted), 2) if drafted else None,
        "rubric_pass_rate": {
            c: round(sum(r.rubric.get(c, False) for r in judged) / len(judged), 3) for c in criteria
        }
        if judged
        else None,
        "mean_rubric_score": round(mean(r.score for r in judged if r.score is not None), 3)
        if judged
        else None,
        "total_cost_usd": round(sum(costs), 4) if costs else None,
    }


async def main(args: argparse.Namespace) -> None:
    s = get_settings()
    setup_tracing("outreach-email-evals", s.logfire_token)
    if not args.sender.exists():
        raise SystemExit(
            f"{args.sender} not found. Copy sender.example.json to sender.json and fill in "
            "your real offer and provable outcomes."
        )
    sender: Sender = load_sender(args.sender)
    if sender.is_placeholder:
        raise SystemExit(f"{args.sender} is the example profile; add your real offer/outcomes")
    judge_model = args.judge_model or s.model_fast
    tracker = SpendTracker(args.max_cost if args.max_cost > 0 else None)
    if tracker.limit_usd is not None and not {s.model_smart, s.model_fast, judge_model} <= set(
        PRICES
    ):
        raise SystemExit("--max-cost needs known prices for every model (see app/pricing.py)")

    companies = load_golden_set()
    if args.domains:
        companies = [c for c in companies if c.domain in set(args.domains)]
    if args.limit:
        companies = companies[: args.limit]

    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
    researcher = ResearchAgent.from_settings(s)
    pp_agent = PainPointAgent(client=client, model=s.model_smart)
    writer = WriterAgent(client=client, model=s.model_smart)
    verifier = VerifierAgent(client=client, model=s.model_fast)

    async def run_pp(lead: Lead, report: ResearchReport) -> PainPointRun:
        return await pp_agent.run(lead, report, sender)

    async def run_draft(lead: Lead, report: ResearchReport, pp: PainPoint) -> DraftResult:
        return await draft_email(writer=writer, verifier=verifier, lead=lead, report=report,
                                 pain_point=pp, sender=sender)  # fmt: skip

    async def run_judge(email: Email, lead: Lead, report: ResearchReport, pp: PainPoint):
        return await judge_email(client=client, model=judge_model, email=email, lead=lead,
                                 report=report, pain_point=pp, sender=sender)  # fmt: skip

    async def one(c: GoldenCompany) -> EmailEvalResult:
        r = await evaluate_email_company(
            c, research=researcher.run, pain_points=run_pp, draft=run_draft, judge=run_judge
        )
        score = f"{r.score:.2f}" if r.score is not None else "-"
        print(f"{r.domain:<18} {r.status:<12} revisions={r.revisions} rubric={score} "
              f"${r.cost_usd or 0:.3f} {r.seconds:>5.0f}s"
              f"{'  ERROR ' + r.error if r.error else ''}", flush=True)  # fmt: skip
        return r

    results = await run_capped(
        companies, one, tracker=tracker, concurrency=args.concurrency,
        cost_of=lambda r: r.cost_usd,
        on_skip=lambda c: EmailEvalResult(c.domain, "skipped_budget", skipped=True),
    )  # fmt: skip
    summary = summarize_emails(results)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"emails-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({
        "config": {"git_commit": _git_commit(), "smart_model": s.model_smart,
                   "fast_model": s.model_fast, "judge_model": judge_model,
                   "sender": sender.company, "max_cost_usd": tracker.limit_usd,
                   "estimated_spend_usd": round(tracker.spent_usd, 4)},
        "summary": summary,
        "results": [asdict(r) | {"score": r.score} for r in results],
    }, indent=2, ensure_ascii=False))  # fmt: skip
    print("\n" + json.dumps(summary, indent=2))
    print(f"\nestimated spend ${tracker.spent_usd:.4f}; wrote {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--domains", nargs="*")
    p.add_argument("--limit", type=int)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--max-cost", type=float, default=1.0, help="USD cap (0 = no cap)")
    p.add_argument("--sender", type=Path, default=Path("sender.json"))
    p.add_argument("--judge-model", help="defaults to MODEL_FAST from config")
    asyncio.run(main(p.parse_args()))
