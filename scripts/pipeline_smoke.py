"""Run the REAL pipeline end to end for a few leads and print what happened.

    uv run python -m scripts.pipeline_smoke                      # built-in test leads
    uv run python -m scripts.pipeline_smoke --max-cost 0.5 posthog.com:"Alex":"Head of Growth"

Same orchestrator + steps the worker runs (validate -> dedupe -> score -> research ->
painpoints -> draft), but in-process against a throwaway SQLite DB, so no Redis or
Postgres is needed. Costs real money (Anthropic + Exa): roughly $0.10-0.15 per lead is
the current estimate. Stops starting new leads once spend reaches --max-cost; a lead
already running can still finish, so the true worst case is cap + one lead.

The sender below is a FAKE test profile: its outcomes are invented, so the emails this
prints must never be sent. It only exercises the pipeline.
"""

import argparse
import asyncio
import tempfile
import time
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db.models import Base, EmailDraft, Run, Step
from app.db.session import make_engine, make_sessionmaker
from app.observability import setup_tracing
from app.pipeline.factory import build_orchestrator
from app.schemas import Lead, Sender

TEST_SENDER = Sender(
    name="Sam Test",
    company="Onboardly",
    offer=(
        "We help B2B software companies find and fix the onboarding steps where new users drop off."
    ),
    outcomes=[  # invented for testing; never send an email that uses these
        "lifted trial-to-paid conversion 18% in 10 weeks for a 40-person analytics startup",
        "cut time-to-first-value from 9 days to 3 for a payroll software company",
    ],
    ask="Open to a 15-minute call next week?",
)

DEFAULT_LEADS = [
    ("posthog.com", "Alex Morgan", "Head of Growth"),
    ("deel.com", "Jordan Lee", "VP Customer Success"),
    ("stripe.com", "Riley Chen", "Director of Product, Onboarding"),
]


async def run_one(orch, sessions, lead: Lead) -> float:
    t0 = time.monotonic()
    run_id = await orch.create_run(lead, TEST_SENDER)
    status = await orch.execute(run_id)
    secs = time.monotonic() - t0

    async with sessions() as s:
        run = await s.get(Run, run_id)
        steps = (await s.scalars(select(Step).where(Step.run_id == run_id)
                                 .order_by(Step.position))).all()  # fmt: skip
        email = await s.scalar(select(EmailDraft).where(EmailDraft.run_id == run_id))

    print(f"\n=== {lead.domain} ({lead.person_role}) -> {status.value} in {secs:.0f}s, "
          f"${run.cost_usd:.4f} ===")  # fmt: skip
    for st in steps:
        err = f"  ERROR: {st.error[:200]}" if st.error else ""
        print(f"  {st.name:<11} {st.status.value:<10} ${st.cost_usd:.4f}{err}")
        out = st.output or {}
        if st.name == "score" and out:
            print(f"    score: {str(out)[:300]}")
        if st.name == "research" and out:
            facts = (out.get("report") or out).get("facts", [])
            print(f"    facts kept: {len(facts)}")
    if email:
        print(f"\n  [{email.status.value}] Subject: {email.subject}\n")
        print("  " + email.body.replace("\n", "\n  "))
        print(f"\n  words: {len(email.body.split())}")
        for p in email.problems:
            print(f"  PROBLEM: {p}")
    return run.cost_usd


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--max-cost", type=float, default=0.75)
    ap.add_argument("leads", nargs="*", help='domain:"Name":"Role"')
    args = ap.parse_args()
    raw = [tuple(x.split(":", 2)) for x in args.leads] or DEFAULT_LEADS
    leads = [Lead(domain=d, person_name=n, person_role=r) for d, n, r in raw]

    settings = get_settings()
    setup_tracing("pipeline-smoke", settings.logfire_token)
    with tempfile.TemporaryDirectory() as tmp:
        engine = make_engine(f"sqlite+aiosqlite:///{Path(tmp) / 'smoke.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = make_sessionmaker(engine)
        orch = build_orchestrator(settings, sessions)

        spent = 0.0
        for lead in leads:
            if spent >= args.max_cost:
                print(f"\nSKIPPED {lead.domain}: spend ${spent:.4f} reached cap ${args.max_cost}")
                continue
            spent += await run_one(orch, sessions, lead)
        print(f"\nTOTAL estimated spend: ${spent:.4f}")
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
