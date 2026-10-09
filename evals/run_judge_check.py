"""Check the email judge itself: does it catch planted flaws, and is it consistent?

    uv run python -m evals.run_judge_check --limit 5 --max-cost 0.30
    uv run python -m evals.run_judge_check --repeats 3 --flaws none --max-cost 0.25

Loads saved emails (default: the 10-lead writer fixture), plants one known flaw per copy
(see evals/email_controls.py) and judges originals + copies `--repeats` times. Only the
judge runs, nothing is drafted. Cost scales with judgements: (originals x (1 + flaws)) x
repeats; one Haiku judgement cost ~$0.007 on 2026-10-09 (estimate from token counts).
`--flaws none` judges only the originals (a pure consistency check).

The fixture was drafted with the FAKE test sender from scripts/pipeline_smoke.py.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import anthropic

from app.config import get_settings
from app.observability import setup_tracing
from app.pricing import PRICES, total_cost_usd
from app.spend import SpendTracker, run_capped
from evals.email_controls import FLAWS, Case, Judged, build_cases, load_cases, summarize
from evals.email_judge import CRITERIA, EmailJudgeError, judge_email
from evals.run_evals import RESULTS_DIR, _git_commit
from scripts.pipeline_smoke import TEST_SENDER

DEFAULT_FIXTURE = RESULTS_DIR / "writer-fixture-10.json"


@dataclass
class Job:
    judged: Judged
    repeat: int


@dataclass
class JobResult:
    job: Job
    passed: dict[str, bool] | None
    reasons: dict[str, str] | None
    cost_usd: float | None
    raw: dict | None = None  # the judge's own output, for debugging fails without re-paying
    skipped: bool = False


async def main(args: argparse.Namespace) -> None:
    s = get_settings()
    setup_tracing("outreach-judge-check", s.logfire_token)
    judge_model = args.judge_model or s.model_fast
    tracker = SpendTracker(args.max_cost if args.max_cost > 0 else None)
    if tracker.limit_usd is not None and judge_model not in PRICES:
        raise SystemExit("--max-cost needs a known price for the judge model (app/pricing.py)")

    originals = load_cases(json.loads(args.fixture.read_text()))
    if args.domains:
        originals = [o for o in originals if o.domain in set(args.domains)]
    if args.limit:
        originals = originals[: args.limit]
    flaws = [] if args.flaws == ["none"] else [
        f for f in FLAWS if not args.flaws or f.name in set(args.flaws)
    ]  # fmt: skip
    cases, not_applied = build_cases(originals, TEST_SENDER, flaws)
    judged = [Judged(c) for c in cases]
    jobs = [Job(j, r) for r in range(args.repeats) for j in judged]
    print(f"{len(originals)} originals, {len(cases) - len(originals)} flawed copies, "
          f"{args.repeats} repeat(s) -> {len(jobs)} judgements; "
          f"cap ${tracker.limit_usd}")  # fmt: skip
    if not_applied:
        print(f"flaws not applicable: {not_applied}")

    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())

    async def one(job: Job) -> JobResult:
        c: Case = job.judged.case
        try:
            score = await judge_email(client=client, model=judge_model, email=c.email,
                                      lead=c.lead, report=c.report, pain_point=c.pain_point,
                                      sender=TEST_SENDER)  # fmt: skip
        except EmailJudgeError as e:
            print(f"  judge error {c.domain}/{c.flaw}: {e}", flush=True)
            return JobResult(job, None, None, None)
        raw = score.agent.output.model_dump() if score.agent and score.agent.output else None
        llm_only = {k: v for k, v in score.passed.items() if k in CRITERIA}
        cost = total_cost_usd([score.agent]) if score.agent else None
        if c.target is not None:
            mark = "caught" if not llm_only[c.target] else "MISSED"
            print(f"  {c.domain:<14} {c.flaw:<24} {mark}", flush=True)
        return JobResult(job, llm_only, score.reasons, cost, raw=raw)

    results = await run_capped(
        jobs, one, tracker=tracker, concurrency=args.concurrency,
        cost_of=lambda r: r.cost_usd,
        on_skip=lambda j: JobResult(j, None, None, None, skipped=True),
    )  # fmt: skip
    for r in results:
        if r.passed is not None:
            r.job.judged.runs.append(r.passed)
        elif not r.skipped:
            r.job.judged.errors += 1
    summary = summarize(judged) | {"skipped_budget": sum(r.skipped for r in results)}

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"judge-check-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({
        "config": {"git_commit": _git_commit(), "judge_model": judge_model,
                   "fixture": str(args.fixture), "repeats": args.repeats,
                   "flaws": [f.name for f in flaws], "max_cost_usd": tracker.limit_usd,
                   "estimated_spend_usd": round(tracker.spent_usd, 4)},
        "summary": summary,
        "judgements": [
            {"domain": r.job.judged.case.domain, "flaw": r.job.judged.case.flaw,
             "target": r.job.judged.case.target, "repeat": r.job.repeat,
             "passed": r.passed, "reasons": r.reasons, "raw": r.raw, "skipped": r.skipped}
            for r in results
        ],
    }, indent=2, ensure_ascii=False))  # fmt: skip
    print("\n" + json.dumps(summary, indent=2))
    print(f"\nestimated spend ${tracker.spent_usd:.4f}; wrote {out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    p.add_argument("--domains", nargs="*")
    p.add_argument("--limit", type=int, help="first N originals only")
    p.add_argument("--flaws", nargs="*", help=f"subset of {[f.name for f in FLAWS]}, or 'none'")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-cost", type=float, default=0.30, help="USD cap (0 = no cap)")
    p.add_argument("--judge-model", help="defaults to MODEL_FAST from config")
    asyncio.run(main(p.parse_args()))
