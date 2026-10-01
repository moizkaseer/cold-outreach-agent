# Roadmap

Claude: keep this file current. Tick boxes when done, and add a dated line to the
Progress log after every working session (what was built, what broke, what's next).

## Week 1 — Fundamentals
- [x] Repo setup: `uv`, ruff, pytest, `.env.example`, `app/config.py`
- [x] Pydantic schemas: `Lead`, `ResearchReport` (facts with `source_url`), `PainPoints`, `Email`, `Verification`
- [x] Raw agent loop on the Anthropic SDK (tools → results → repeat, turn + token budget)
- [~] Golden set: 30 real companies with hand-verified facts in `evals/golden_set/`
  (30 companies / 101 facts drafted with verbatim quotes, all quotes machine-checked;
  **human verification pending** — see `evals/golden_set/README.md`)

## Week 2 — Research agent
- [ ] Tools: `search` (behind interface), `fetch_page` (httpx + trafilatura), `finish`
  (`search` = Exa behind `SearchProvider` in `app/tools/search.py`; `fetch_page` and
  `finish` done earlier — research agent wiring next)
- [x] Research agent returns cited `ResearchReport` (built + unit-tested; first real run pending API keys)
- [x] `evals/run_evals.py`: fact accuracy + citation validity vs golden set (built + tested;
  no real eval run yet — needs API keys and verified golden facts)

## Week 3 — Rest of the chain
- [x] Pain point agent (sequential, evidence-based) — unit-tested with fakes; no real run yet
- [x] Writer agent (email rules from CLAUDE.md) — unit-tested with fakes; no real run yet
- [x] Verifier + revise loop (max 2) — unit-tested with fakes; no real run yet
- [x] LLM-as-judge rubric for email quality in evals (built + tested; uncalibrated, never run for real)

## Week 4 — Backend
- [x] Postgres models: runs, steps, leads, research cache, emails (+ Alembic) — tested on
  SQLite only; must run `alembic upgrade head` against Supabase before deploy
- [x] Orchestrator state machine with resume-from-failed-step (fakes + SQLite; no real run yet)
- [x] Redis + Arq worker (job + enqueue tested with fakes; no real Redis run yet)
- [x] FastAPI: create run, get run, SSE progress stream (+ resume) — in-process tests; no auth yet

## Week 5 — Production hardening
- [ ] Tracing (Langfuse or Logfire) with cost per run
- [ ] Per-domain research cache, prompt caching, model routing
- [ ] Prompt-injection test pages in evals

## Week 6 — Ship the demo
- [ ] Connect "Run" button on moizali.vercel.app
- [ ] Rate limiting + daily spending cap (`app/spend.py` SpendTracker exists; used by evals)
- [ ] Docker + deploy

## Week 7 — Close the loop
- [ ] Review UI (approve / edit / reject)
- [ ] Sending integration (after approval only)
- [ ] Connect reply-classification workflow

## Week 8 — Discovery and story
- [ ] Contact discovery via provider API
- [ ] (Optional) Claude Agent SDK research implementation, compared on evals
- [ ] Rewrite case study with real metrics from evals

## Progress log
- 2026-10-01 — Step 1 done: uv project (Python 3.12), ruff + pytest config, typed
  `app/config.py` (pydantic-settings, SecretStr key, model IDs + research budget),
  `.env.example`, `.gitignore`, 3 config tests passing. Moved ROADMAP.md into `docs/`
  so CLAUDE.md's `@docs/ROADMAP.md` import resolves. Next: Pydantic schemas.
- 2026-10-01 — Step 2 done: schemas in `app/schemas/` (`Lead`, `Fact`/`ResearchReport`,
  `PainPoints`, `Email`, `Verification`) on a shared `StrictModel` (extra=forbid). Grounding
  enforced in types: every fact needs `source_url` + verbatim `evidence`; pain points and
  emails reference fact ids; `Verification.passed` is derived, not LLM-set; email word limit
  enforced in code. Caught a bug: `computed_field` broke dump→reload under extra=forbid
  (would break resume) — switched to plain properties + round-trip tests. 23 tests passing.
  Next: raw agent loop on the Anthropic SDK.
- 2026-10-01 — Step 3 done: generic agent loop in `app/agents/loop.py` (own loop on the
  async Anthropic SDK, no framework). `Tool` = name + Pydantic input model + async handler;
  `finish` tool generated from the output model. Hard stops: max turns, token budget
  (checked before each call), refusal, truncation, invalid output after 1 retry, no-finish
  after 2 nudges. Tool errors/bad inputs go back as `is_error` results; parallel calls run
  concurrently, results in one message. Server-side refusal fallback enabled. Returns
  `AgentResult` with transcript + tool-call log (for evals/tracing). 15 loop tests with a
  scripted fake client (38 total). NOT yet run against the real API — needs a key in `.env`
  (`uv run python -m scripts.loop_smoke`). Next: golden set.
- 2026-10-02 — Step 4 (golden set) drafted, not yet verified. Built `evals/golden.py`
  (one JSON per company; facts `verified: false` until a human checks them; evals use
  verified facts only), `app/tools/fetch.py` (httpx + trafilatura; SSRF guard on every
  redirect hop; size/type limits) and `app/tools/text.py` (formatting-tolerant verbatim
  quote matching), plus `scripts/check_golden.py`. Drafted 30 companies / 101 facts from
  live About pages; 0 missing quotes. Real-world findings for the research agent: 5 sites
  403 our fetcher, JS-rendered numbers vanish (GitLab), pages contradict themselves (Deel),
  About pages embed stale third-party press (Wise). 56 tests passing.
  Next: Moiz verifies facts; then Week 2 (search tool + research agent + run_evals.py).
- 2026-10-02 — Step 5: `app/tools/search.py` — `SearchProvider` protocol + `ExaSearch`
  (REST via httpx, checked against Exa docs; returns provider cost for tracing). Search
  results are discovery-only; facts must cite fetched pages. `EXA_API_KEY` optional in
  config. 63 tests passing. Not yet run against real Exa (no key yet).
- 2026-10-02 — Step 6: research agent in `app/agents/research/` (static cacheable system
  prompt in `prompts.py`; tools = search, fetch_page, finish only). Code re-checks
  grounding after `finish`: fact kept only if its source_url was fetched this run AND the
  evidence quote is in the text the model saw; others dropped with a reason. Tool output
  wrapped as `<untrusted_content>`. Prompt caching on in the loop; token budget raised to
  150k (history is re-sent each turn); evidence capped at 500 chars. `Researcher` protocol
  for swapping implementations. 72 tests. Real run: `scripts/research_smoke.py` (needs
  ANTHROPIC_API_KEY + EXA_API_KEY). Next: run_evals.py.
- 2026-10-02 — Step 7: `evals/run_evals.py` + `evals/judge.py`. Metrics: grounding (code:
  kept/submitted facts), faithfulness (Haiku judge: claim matches its quote), coverage
  (Haiku judge: verified golden facts found), plus finished-rate, turns, latency and
  estimated cost (`app/pricing.py`, prices as of 2026-09-25). Results saved to
  `evals/results/<timestamp>.json` with git commit + config. Default scores only verified
  golden facts; `--include-unverified` for dev runs. Loop gained `refusal_fallback` flag
  (off for the Haiku judge). 82 tests. NO real metrics yet.
  Next: add keys, run research_smoke on 1-2 companies, then a small eval run.
- 2026-10-02 — First real runs (keys added). Loop smoke OK (prompt caching confirmed).
  Research smoke on posthog.com: 27s, 4 turns, 8 grounded facts, ~$0.07 (my estimate from
  token counts). Eval, 3 companies (posthog/stripe/deel), scored vs UNVERIFIED golden
  facts — tiny sample, treat as indicative only:
  - run 1 `evals/results/20261002-004931.json`: grounding 1.00, faithfulness 0.54,
    coverage 0.25, 4.3 turns, ~$0.047/company. Bug: first `finish` failed in 3/3 runs
    (model sent `facts` as a malformed string).
  - fix: `strict: true` on `finish` (schema copy stripped of unsupported keywords; Pydantic
    still enforces them) + prompt "one fact, one quote; quote must be self-explanatory".
  - run 2 `evals/results/20261002-005112.json`: finish OK first try 3/3; 3.0 turns;
    faithfulness 0.58 (within noise); grounding 0.68 because deel URLs had trailing
    debris (`/','`) -> 8 facts correctly rejected. Added debris-tolerant lookup (cleaned URL
    must still match a fetched page). Also caught one genuinely unfetched citation.
  - Remaining issue: model merges details into claims beyond its single quote.
    Next experiment: allow 1-3 verified quotes per fact, then re-measure.
- 2026-10-02 — Experiment: facts carry 1-3 verbatim quotes from one page (every quote
  code-checked; quotes together must support the whole claim). Judge prompt updated to
  "quotes, taken together" (same standard; note the instrument changed slightly).
  run 3 `evals/results/20261002-005546.json` (same 3 companies, unverified golden):
  grounding 0.96, faithfulness 0.92 (judge), coverage 0.50, 3.0 turns, ~$0.054/company.
  Quotes per fact: 1 quote x7, 2 x14, 3 x3. CAVEAT: manual review of the 24 kept facts
  found ~6 the judge wrongly passed (dates or numbers not in any quote, e.g. "500,000+
  teams", "Sep 29, 2025"), so true faithfulness is closer to ~0.75 by that review. The
  judge looks more lenient with multiple quotes. Next: make the judge decompose claims
  into atomic details and calibrate it against human labels before trusting its numbers.
- 2026-10-02 — Judge v2 + calibration tooling. Judge now decomposes each claim into atomic
  details and names the supporting quote for each (or null); CODE derives faithful = every
  detail has a valid quote (out-of-range quote numbers never count). Each fact judged only
  against its own quotes. `--judge-model` flag on run_evals. New `evals/calibration.py`:
  `export` (results -> labels CSV, refuses to overwrite labels) and `score` (agreement,
  false-pass/false-fail rates, Cohen's kappa, disagreement list). 101 tests.
  Sanity check on the same 24 facts from run 3 (NOT calibration; no human labels yet):
  old Haiku judge 0.92, new Haiku 0.67 (~$0.02), new Sonnet 0.42 (~$0.06). Judge choice
  alone moves faithfulness by 50 points -> must calibrate before quoting any number.
  `evals/calibration/labels.csv` (24 facts) exported, awaiting Moiz's labels.
- 2026-10-02 — Cost guard: `app/spend.py` SpendTracker; `run_evals --max-cost` (default
  $1.00, 0 = off) stops new companies once estimated spend reaches the cap; refuses to run
  with a cap if model prices are unknown; skipped companies excluded from metrics. Caught
  a float bug (0.04+0.01+0.04+0.01 < 0.10). Session spend so far ≈ $0.67 estimated
  (≈$0.60 Anthropic + ≈$0.07 Exa). From now on: no real API calls without Moiz's OK.
- 2026-10-02 — Step: pain point agent (`app/agents/painpoints/`). Added `Sender` schema +
  `sender.example.json` (gap in CLAUDE.md: the writer's "one concrete outcome" needs real,
  sender-provided outcomes; models may only use these). Pain points are framed as
  hypotheses citing fact ids; code drops any that cite unknown ids and re-ranks. Shared
  `app/agents/formatting.py` keeps fact quotes wrapped as untrusted data downstream.
  109 tests. No API calls (Moiz's request). Moiz to provide a real sender profile.
- 2026-10-02 — Step: writer agent (`app/agents/writer/`). Code-checked rules in
  `rules.py`: <120 words (schema, retried by loop), known fact ids, banned openers, no
  AI/automation words (subject too), plain text (no markdown/bullets/links/HTML), and the
  sender's ask included verbatim. Judgement rules (specific opener, one pain point,
  outcome from sender list) left to verifier/evals. Supports revisions (previous draft +
  problems). 124 tests. Open question for Moiz: "never mention AI" also blocks openers about
  AI companies' own products (e.g. ElevenLabs) — kept strict for now.
- 2026-10-02 — Step: verifier (`app/agents/verifier/`) + revise loop
  (`app/agents/drafting.py`). Verifier maps each factual claim to a source (fact id +
  quote number, or sender outcome number); CODE decides support and writes the writer
  feedback (same design as judge v2). "No claims found" is an error, not a pass. Loop:
  write -> code lint (fail = revise without paying for verifier) -> verify -> revise, max
  2 revisions -> VERIFIED / NEEDS_HUMAN (last draft + problems kept) / FAILED. Nothing is
  sent; all results go to human review. 134 tests, fakes only.
  Week 3 remaining: LLM-as-judge email-quality rubric in evals. Verifier uses Haiku per
  CLAUDE.md; revisit after judge calibration (Haiku was lenient as a fact judge).
- 2026-10-02 — Email rubric judge (`evals/email_judge.py`): 6 binary LLM criteria
  (specific opener, single pain point, pain as hypothesis, one sender outcome, relevant to
  role, human tone) + the code-checked rules as a 7th. Passes that need evidence must quote
  the email verbatim (code-checked; fake quotes turn a pass into a fail). AgentResult now
  records its model; `app/pricing.total_cost_usd` prices mixed-model runs (None if any
  price unknown). 141 tests. Needs calibration before its numbers are quoted.
- 2026-10-02 — End-to-end email eval (`evals/run_email_evals.py`): research -> pain points
  -> write/verify/revise -> rubric per company, under the shared spend cap
  (`app/spend.run_capped`, now also used by run_evals). Reports status mix, verified rate,
  avg revisions, rubric pass rate per criterion, mixed-model cost. Refuses the example
  sender profile (`Sender.is_placeholder`) and explains a missing sender.json. 146 tests,
  fakes only. Week 3 code complete; nothing in Week 3 has run against the real API.
  Blocked on Moiz for real numbers: sender.json, 24 calibration labels, golden verification.
  Next (no API needed): Week 4 — Postgres models + Alembic, orchestrator with resume.
- 2026-10-02 — Week 4 step 1: DB layer. `app/db/models.py` (leads, runs, steps with
  unique (run_id, name), research_cache, emails with review status), portable types (Uuid,
  JSON->JSONB on PG, tz-aware DateTime, enums stored as strings), `app/db/session.py`,
  async Alembic in `app/db/migrations` (initial migration; fixed autogenerate's missing
  `Text` import). `DatabaseSettings` split out so migrations need no API keys;
  DATABASE_URL defaults to local SQLite. Found SQLite drops timezones -> `as_utc()` helper.
  152 tests. NOT yet run on Postgres. Next: orchestrator state machine with resume.
- 2026-10-02 — Week 4 step 2a: orchestrator engine (`app/pipeline/orchestrator.py`).
  create_run snapshots the sender + creates pending step rows; execute walks steps in
  order, persisting running/succeeded/failed + output + cost per step; finished steps are
  skipped on resume (saved outputs reloaded, nothing paid twice); steps can STOP a run
  deliberately; last step can set final status. Runs are claimed via optimistic-lock
  UPDATE (two workers can't run the same run; verified by a concurrency test); RUNNING runs
  older than 15 min are reclaimable. 7 engine tests (159 total). Known gap: cost spent by a
  step that then raises isn't recorded yet — handle in 2b.
- 2026-10-02 — Week 4 step 2b: real steps (`app/pipeline/steps.py`) validate -> dedupe ->
  score -> research -> painpoints -> draft, wired by `app/pipeline/factory.py`. Cheap
  gates first: placeholder sender stops at validate; same person+domain within 30 days
  stops at dedupe; Haiku `LeadScorer` (new, `app/agents/scorer/`) stops fit < 3/5 before
  research is paid for. Research uses the 7-day per-domain cache (only reports with facts
  are cached). Draft saves an `emails` row (verified / needs_human) for human review.
  `StepFailed(cost_usd=...)` records money spent before a failure (closes 2a gap).
  170 tests. Next: Redis + Arq worker, then FastAPI + SSE.
- 2026-10-02 — Week 4 step 3: Arq worker (`app/workers/worker.py`: job = orchestrator
  .execute(run_id); job_timeout 600s, max_jobs 4, max_tries 1 because failures are
  persisted and resumed explicitly) + `app/workers/queue.py` (deterministic job ids make
  enqueue idempotent: double-clicks can't start a run twice). `InfraSettings` (DB + Redis
  URLs, no API keys) replaces DatabaseSettings. 173 tests. Redis isn't installed locally,
  so no real queue round-trip yet. Run: `uv run arq app.workers.worker.WorkerSettings`.
  Next: FastAPI (create run, get run, SSE progress, resume).
- 2026-10-02 — Week 4 step 4: FastAPI (`app/api/main.py`): POST /runs (validates +
  normalises lead, refuses without a real sender profile, enqueues; 202), GET /runs/{id}
  (status, steps, email, cost), GET /runs/{id}/events (SSE: polls DB, sends only changes,
  heartbeat, ends on terminal status), POST /runs/{id}/resume (FAILED only; new job id per
  attempt), GET /health. Tests caught: 422 for a bad domain was a 500 (unserialisable
  error context), and a blocking Path.exists() in async startup. `create_run` extracted +
  `STEP_NAMES` (test keeps it in sync). 183 tests.
  WEEK 4 CODE COMPLETE — but never run end-to-end for real (no Redis locally, no Postgres
  run, no real LLM calls since the user paused API spend). No auth/rate limits: not
  deployable publicly until Week 6.
