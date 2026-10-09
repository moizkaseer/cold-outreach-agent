"""Re-draft emails from SAVED research and score them, to compare writer changes cheaply.

    uv run python -m scripts.pipeline_smoke --save evals/results/smoke.json   # once
    uv run python -m scripts.redraft evals/results/smoke.json --baseline      # score saved
    uv run python -m scripts.redraft evals/results/smoke.json                 # new drafts

Research is ~65% of a lead's cost and varies run to run. Holding it fixed means a
before/after difference comes from the writer, not from different facts. Only the
write -> verify -> revise loop and the rubric judge run here (~$0.02 per lead, estimate).
Uses the same FAKE test sender as the smoke: never send these emails.

n is tiny and the rubric judge is uncalibrated: treat results as a direction, not a score.
"""

import argparse
import asyncio
import json
from pathlib import Path

import anthropic

from app.agents.drafting import draft_email
from app.agents.verifier import VerifierAgent
from app.agents.writer import WriterAgent
from app.config import get_settings
from app.pricing import total_cost_usd
from app.schemas import Email, Lead, PainPoint, ResearchReport
from evals.email_judge import EmailJudgeError, judge_email
from scripts.pipeline_smoke import TEST_SENDER


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", type=Path)
    ap.add_argument("--baseline", action="store_true", help="score the saved drafts only")
    args = ap.parse_args()

    s = get_settings()
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
    writer = WriterAgent(client=client, model=s.model_smart)
    verifier = VerifierAgent(client=client, model=s.model_fast)

    totals: dict[str, list[bool]] = {}
    spent = 0.0
    for rec in json.loads(args.path.read_text()):
        if not (rec.get("report") and rec.get("pain_point")):
            continue
        lead = Lead.model_validate(rec["lead"])
        report = ResearchReport.model_validate(rec["report"])
        pp = PainPoint.model_validate(rec["pain_point"])

        if args.baseline:
            email, status, revisions = Email.model_validate(rec["email"]), "saved", 0
        else:
            d = await draft_email(writer=writer, verifier=verifier, lead=lead, report=report,
                                  pain_point=pp, sender=TEST_SENDER)  # fmt: skip
            spent += total_cost_usd(a for att in d.attempts for a in att.agents) or 0.0
            if d.email is None:
                print(f"\n=== {lead.domain}: draft FAILED ({d.reason})")
                continue
            email, status, revisions = d.email, d.status.value, d.revisions

        try:
            score = await judge_email(client=client, model=s.model_fast, email=email,
                                      lead=lead, report=report, pain_point=pp,
                                      sender=TEST_SENDER)  # fmt: skip
        except EmailJudgeError as e:
            print(f"\n=== {lead.domain}: JUDGE FAILED ({e})\n{email.body}")
            continue
        spent += total_cost_usd([score.agent]) or 0.0

        print(f"\n=== {lead.domain} [{status}, {revisions} revisions] "
              f"rubric {score.score:.2f} ===")  # fmt: skip
        print(f"Subject: {email.subject}\n\n{email.body}\n\nwords: {len(email.body.split())}")
        for c, ok in score.passed.items():
            totals.setdefault(c, []).append(ok)
            if not ok:
                print(f"  FAIL {c}: {score.reasons.get(c, '')}")

    print("\n--- pass rate per criterion ---")
    for c, oks in totals.items():
        print(f"  {c:<20} {sum(oks)}/{len(oks)}")
    print(f"\nestimated spend: ${spent:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
