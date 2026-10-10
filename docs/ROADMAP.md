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
- [x] Tools: `search` (behind interface), `fetch_page` (httpx + trafilatura), `finish`
  (`search` = Exa behind `SearchProvider` in `app/tools/search.py`; all three wired into
  the research agent and used in real runs since 2026-10-02)
- [x] Research agent returns cited `ResearchReport` (built + unit-tested; first real run pending API keys)
- [x] `evals/run_evals.py`: fact accuracy + citation validity vs golden set (built + tested;
  no real eval run yet — needs API keys and verified golden facts)

## Week 3 — Rest of the chain
- [x] Pain point agent (sequential, evidence-based) — first real runs 2026-10-09
- [x] Writer agent (email rules from CLAUDE.md) — first real runs 2026-10-09
- [x] Verifier + revise loop (max 2) — first real runs 2026-10-09
- [x] LLM-as-judge rubric for email quality in evals (built + tested; uncalibrated, never run for real)

## Week 4 — Backend
- [x] Postgres models: runs, steps, leads, research cache, emails (+ Alembic) — applied to
  Supabase 2026-10-07 (schema + RLS at head); app run against local Postgres 16 2026-10-10
- [x] Orchestrator state machine with resume-from-failed-step (fakes + SQLite; no real run yet)
- [x] Redis + Arq worker — real Redis round trip 2026-10-10 (`scripts/infra_smoke.py`)
- [x] FastAPI: create run, get run, SSE progress stream (+ resume) — run as a real server
  against Postgres + Redis 2026-10-10; no auth yet

## Week 5 — Production hardening
- [x] Tracing (Langfuse or Logfire) with cost per run — Logfire; tested in-memory, no account yet
- [x] Per-domain research cache, prompt caching, model routing (cache: Week 4 step 2b; prompt caching: loop; routing: Sonnet research/writing, Haiku scoring/verification)
- [x] Prompt-injection test pages in evals (built + tested with fakes; never run against the real model)

## Week 6 — Ship the demo
- [ ] Connect "Run" button on moizali.vercel.app
- [x] Rate limiting + daily spending cap (`app/api/limits.py`; DB-backed; atomic via a
  Postgres advisory lock; burst-tested on real Postgres 2026-10-10)
- [x] Auth: API keys, per-team ownership of runs and emails, CORS (2026-10-10)
- [~] Docker + deploy (image builds and the full stack boots locally via `docker compose`,
  2026-10-10; deploy itself needs Moiz's accounts)

## Week 7 — Close the loop
- [~] Review UI (approve / edit / reject) — API done 2026-10-10 (`app/api/review.py`);
  no frontend yet
- [~] Sending integration (after approval only) — built 2026-10-10 behind an interface
  (`app/sending/`, `app/api/sending.py`) with unsubscribe + suppression list; tested with a
  fake provider only, NEVER run against Resend (needs an account + sending domain)
- [ ] Connect reply-classification workflow

## Week 8 — Discovery and story
- [ ] Contact discovery via provider API (Apollo/Hunter; find the right person to contact)
- [ ] Fit signal: does the prospect already use the seller's product or a competitor's?
  (e.g. tech-stack evidence, cited like every other fact)
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
- 2026-10-02 — Week 5 step 1: tracing with Logfire (`app/observability.py`), chosen over
  Langfuse: OpenTelemetry-based (not vendor-locked), covers API + worker, sends nothing
  without a token. Spans: pipeline run -> step -> agent -> llm call / tool, metadata only
  (model, turn, stop reason, tokens, estimated cost, tool name, error flag). Prompts and
  page text deliberately NOT traced (a test asserts canary strings never appear); that's
  why logfire.instrument_anthropic isn't used. Setup in worker, API (+ FastAPI request
  spans) and eval runners; LOGFIRE_TOKEN optional. Moved `Usage` to `app/agents/usage.py`
  to break a pricing<->loop import cycle. 186 tests. Next: prompt-injection eval pages.
- 2026-10-02 — Week 5 step 2: prompt-injection evals (`evals/injection/cases.py`,
  `evals/run_injection_evals.py`). Fictional "Brightloom" pages served by a mock HTTP layer
  (no real web traffic): direct instruction, display:none text, white-on-white text,
  wrapper-escape (fake </untrusted_content>), system-prompt exfiltration; plus a writer case
  (poisoned research quote adding a link + fake discount). Canary strings detect success;
  results split into `passed` (nothing reached output) and `model_followed` (model tried,
  even if code then blocked it). Meta-tests prove each attack reaches the model and that an
  obedient fake model is caught. Findings: (1) trafilatura strips display:none text, a free
  defence (pinned so an upgrade can't silently remove it); (2) KNOWN LIMITATION: grounding
  proves a quote is on the page, not that the page is honest — injected text can be quoted
  and pass grounding; only the model's judgement + this eval guard it. 197 tests.
  WEEK 5 CODE COMPLETE. Run (costs ~$0.20-0.40, my estimate): `uv run python -m
  evals.run_injection_evals --max-cost 0.50`.
- 2026-10-04 — Week 6 step 1: public-demo guards (`app/api/limits.py`). Per-client rate
  limit (default 3 new runs/hour, keyed by a hashed IP stored in new `runs.client_key`,
  migration `95b7c19827fa`) -> 429 + Retry-After. Daily spending cap (default $2/UTC day,
  estimated): spend = cost of steps that FINISHED today + a worst-case reserve ($0.50) per
  in-flight run, since in-flight spend isn't in the DB yet; runs untouched for 1h stop
  holding a reserve. New runs and resumes refused with 503 once the cap would be crossed.
  Both answered from the DB (no new infra; survives restarts). Known gaps: check+insert
  isn't atomic (simultaneous requests can overshoot by ~1 run); IP-based limits are weak
  against rotating IPs; still no auth. Behind a proxy uvicorn needs `--proxy-headers`.
  Migration tested up/down on SQLite only. 205 tests. Next: Dockerfile + deploy.
- 2026-10-04 — Week 6 step 2: `Dockerfile` + `.dockerignore`. One image (uv, lockfile,
  --no-dev, non-root) with three commands: API (default; uvicorn on $PORT with
  --proxy-headers, FORWARDED_ALLOW_IPS=* so the rate limit sees real client IPs behind the
  host's proxy), worker (`arq app.workers.worker.WorkerSettings`), migrations (`alembic
  upgrade head` as a release command, not on boot, so replicas can't race). NOT BUILT:
  Docker isn't installed here. Approximated instead: copied only what .dockerignore allows,
  fresh `uv sync --frozen --no-dev`, both entrypoints import, migrations run, pytest absent.
  Next (needs Moiz): install Docker and build; pick host (Railway/Render/Fly) + Supabase +
  managed Redis; create sender.json; then wire the Run button on moizali.vercel.app.
- 2026-10-04 — Migration `b3f1a2c4d5e6`: enable RLS (no policies) on all tables on Postgres,
  so Supabase's auto REST API (public anon key) can't read/write our data; the app connects
  as table owner and is unaffected. No-op on SQLite. Tested up/down on SQLite only; RLS
  itself not yet verified on Supabase. Moiz created the Supabase project; next: set
  DATABASE_URL (session pooler) and run `alembic upgrade head`.
- 2026-10-07 — Status check: 205 tests pass, ruff clean. Applied all 3 migrations to the
  Supabase project (now at head `b3f1a2c4d5e6`). This network blocks outbound TCP 5432/6543
  (checked against an unrelated host too), so `alembic upgrade head` can't connect from this
  machine. Instead: `alembic upgrade head --sql` (offline mode) -> reviewed SQL -> applied via
  the Supabase connector over HTTPS, including the `alembic_version` row, so future
  `alembic upgrade` runs from the deploy host see the DB as current. Security advisor
  confirms RLS on all 6 tables with no policies (deny-all for the anon key, as intended).
  Advisor also flags `public.rls_auto_enable()` (SECURITY DEFINER, callable by anon). Not
  ours; looks Supabase-created. Left untouched for Moiz to decide. Docker is now installed but
  the user isn't in the `docker` group yet (`sudo usermod -aG docker $USER`, then re-login).
  Still blocked on Moiz: golden-fact verification (0/101), 24 calibration labels,
  sender.json. Next (no API spend): Week 7 review API (approve / edit / reject).
- 2026-10-09 — Product direction (Moiz): users are SALES TEAMS (SDRs). The agent replaces
  their manual research (company info, "do they use our/competitor product", right contact;
  today they patch this with Apollo). So the sender is whoever runs it, not a fixed profile.
  Launch step 1: `POST /runs` now takes an optional `sender` (name, company, offer, provable
  outcomes, ask); request wins, `sender.json` is only a single-tenant fallback; missing or
  placeholder -> 422 (was 503). Sender fields length-capped (offer 400, <=5 outcomes x 200,
  ask 150) since strangers' text reaches the writer/verifier prompts; it never reaches the
  research agent and no agent has outward-acting tools. Run rows already snapshot the
  sender, so the pipeline needed no change. 213 tests. Added Week 8 items: Apollo contact
  discovery, product/competitor-usage fit signal. Next: launch step 2, first real
  end-to-end run (needs a real test sender profile + Moiz's OK for ~$0.50 API spend).
- 2026-10-09 — Launch step 2 started: `scripts/pipeline_smoke.py` runs the REAL
  orchestrator + steps in-process on a throwaway SQLite DB (no Redis), with a FAKE test
  sender (invented outcomes; emails must never be sent), spend cap (default $0.75). First
  attempt: all 3 leads failed at `score` with 401 invalid x-api-key, $0 spent. The key in
  .env has prefix `sk-ant-usr` (not a Console API key, `sk-ant-api...`). Resume orchestration
  worked as designed (validate/dedupe succeeded, later steps left pending). Blocked on a
  valid ANTHROPIC_API_KEY; then rerun the smoke.
- 2026-10-09 — FIRST REAL END-TO-END RUN (`scripts/pipeline_smoke.py`, fake test sender,
  in-process SQLite, no Redis). Key fixed (old key was invalid; the `sk-ant-usr` prefix
  was NOT the cause, my earlier guess was wrong). 3/3 leads succeeded, 3/3 emails
  `verified`, 84-96 words. Measured (estimated from tokens + Exa cost): posthog $0.082 / 78s,
  deel $0.080 / 34s, stripe $0.109 / 43s; total $0.271, avg ~$0.09/lead. Research is
  60-70% of cost ($0.049-0.076); score ~$0.002; painpoints ~$0.013-0.016; draft
  ~$0.014-0.016. Scores 4/4/5. My read of the emails (n=3, not an eval): grounded and
  specific; deel email crams 4 stats into 2 sentences; stripe email has an awkward hedge
  ("I can't see your data"). Next: tune writer prompt for one stat + natural hypothesis
  tone (measure with email rubric), then Docker build + deploy.
- 2026-10-09 — Writer tuning loop (cheap): `pipeline_smoke --save` stores research + drafts;
  new `scripts/redraft.py` re-drafts from the SAME research and scores with the rubric
  (`--baseline` scores saved drafts), so writer changes cost ~$0.07 for 3 leads instead
  of ~$0.28 and research variance is held fixed. Rubric gained `few_numbers`, a CODE check
  (<=2 company numbers; sender's own outcome/ask numbers excluded). Fixed a real judge bug:
  Haiku returned an empty results list -> crash; `EmailJudgeOutput.results` now requires
  exactly one row per criterion so the loop retries. Writer prompt: at most one company
  number; pain framed inside the sentence, no "I can't see your data" disclaimers.
  Fixture `evals/results/smoke-20261009.json` (smoke run 2: $0.280, 3/3 verified).
  Rubric, n=3, uncalibrated judge: baseline 23/24 -> after 23/24 (failure moved: pain
  hypothesis false-fail -> stripe opener quotes product marketing, a fair hit). Emails
  shorter (79-86 vs 85-103 words), one company number each. The first run's issues did
  not recur in run 2's baseline, so they're intermittent; n=3 can't show improvement.
  Spend 2026-10-09 ≈ $0.62 estimated (2 smoke runs $0.55 + judging/redraft $0.08).
  Next ideas: prefer news (launch/hire/funding) over marketing copy for the opener;
  grow the fixture to ~10 leads before trusting rubric deltas; then Docker + deploy.
- 2026-10-09 — Fixture grown to 10 leads (`evals/results/writer-fixture-10.json`; 7 new
  leads via smoke run 3, $0.753). 7/7 new leads verified with 0 revisions; cost
  $0.097-0.138/lead (webflow highest), 40-59s. Score gate works: allbirds.com (retail,
  deliberate poor fit) stopped at score 1/5 for $0.0013, no research paid.
  NOTE: fixture mixes drafts from two writer prompts (3 old, 7 new).
  Rubric baseline on all 10: 80/80 (judge $0.066). The judge is SATURATED: the stripe
  opener it failed earlier today passed now, so it's inconsistent and can't separate good
  from bad. Further writer tuning against it is pointless until it's calibrated.
  My read of the 7 new emails: openers are real news (launches, GA, milestones), grounded;
  but formulaic ("teams ... often find ... stall" in ~all), "caught my eye" x2, and mercury
  used 2 company numbers despite the one-number instruction (code check allows <=2).
  Next: Moiz labels the 10 emails (send / don't send + why) -> calibrate or replace the
  judge (stricter prompt, maybe Sonnet), then tune. Spend 2026-10-09 ≈ $1.44 estimated.

- 2026-10-09 — Judge check without expert labels (`evals/email_controls.py`,
  `evals/run_judge_check.py`). Moiz isn't an SDR, so his send/don't-send labels would
  calibrate the judge to the wrong taste; `email_labels.csv` is shelved until 2-3 real
  sales people can label it. Instead: 7 planted flaws, each a deterministic edit aimed at
  one LLM criterion (generic opener, extra pain point, pain stated as fact, changed outcome
  number, vague outcome, off-target role "General Counsel", salesy hype). The correct
  verdict is known by construction. Reports catch rate per flaw, pass rate on unedited
  originals ("no planted flaw", not "known good"), and flip rate across `--repeats`.
  All 7 flaws apply to all 10 fixture emails (70 copies). Edits that would push an email
  over 120 words are skipped (the writer can't produce those). 228 tests, no API spend.
  Real ground truth later = reply rates once sending exists (Week 7).
  Next (needs Moiz's OK): `--limit 5` run (~40 judgements, ~$0.28 est.) and a
  `--flaws none --repeats 3` consistency run (~30 judgements, ~$0.21 est.).
- 2026-10-09 — First judge-check runs (Haiku judge; n=5 per flaw, so directional only).
  `judge-check-20261009-224559.json` (5 emails + 35 flawed copies, $0.223): mean catch
  rate 0.66. Caught every time: changed outcome number 5/5, vague outcome 5/5, salesy hype
  5/5. Weak: generic opener 1/5, wrong role 1/5, extra pain point 3/5, pain as fact 3/5.
  `judge-check-20261009-224705.json` (10 originals x3, $0.169): overall flip rate 0.017
  (only pain_as_hypothesis flipped, on stripe). So the judge is CONSISTENT BUT LENIENT;
  my earlier "inconsistent" read (one stripe flip) was mostly wrong. Its reasons show a
  pass-seeking pattern: it finds any passing detail and ignores the planted flaw (generic
  first sentence -> "references Anytime Pay" from sentence 2; rationalises General Counsel
  caring about activation). Caveats: the stripe/wrong_role case is not clean (KYC and
  compliance are partly a legal concern); the stripe original's pain_as_hypothesis fail
  is our evidence rule rejecting a quote stitched from two places, not the model.
  Spend 2026-10-09 ≈ $1.83 estimated.
  Next: fix by design rather than prompt pleading: code hands the judge the first sentence
  for the opener check; pain criteria ask the judge to LIST every pain point/assertion
  (code counts them); role check asks which role owns the pain. Then re-run the same check.
- 2026-10-09 — Email judge v2 (`evals/email_judge.py`), fixing the pass-seeking pattern by
  design: specific_opener sees only the code-extracted opening sentence (evidence must be
  inside it); single_pain_point + pain_as_hypothesis are DERIVED BY CODE from a list of
  every problem the judge finds (fail-safe: flagged problems count even with sloppy quotes,
  passes need a verbatim quote); relevant_to_role must first name the pain's owner roles.
  Re-run `judge-check-20261009-225325.json` (same 5 emails, $0.246): catch rate 35/35
  (was 23/35). But it OVERSHOT on originals: opener 4/5, single pain 3/5, hypothesis 2/5.
  My read of the 6 original fails: 1 fair (stripe opener quotes product marketing, which I
  flagged earlier too), 5 false (elaboration sentence listed as a second problem; opener
  fact listed as a problem; sub-sentence quote dropped the "often find" hedge).
  v2.1 (prompt only, not yet measured): tighter "problem" definition, whole-sentence quotes.
  Overfitting risk: tuned on these 5 emails, so the next run should use the 5 HELD-OUT
  fixture emails (webflow, retool, sentry, mercury, cal) as well. Spend 2026-10-09 ≈ $2.08.
- 2026-10-09 — Judge v2.1 measured, `judge-check-20261009-230039.json` (all 10 emails, 70
  flawed copies, $0.520). Catch rate 69/70 (0.986); dev half 34/35, HELD-OUT half 35/35, so
  no sign the prompt fix overfit. Only miss: stripe/wrong_role, the case already noted as
  not clean (the judge argued a General Counsel owns KYC/compliance; defensible). Originals:
  57/60 criterion passes (was 25/30 on the dev half under v2). The 3 fails, my read:
  stripe opener = fair (product marketing copy); linear opener ("homepage now puts Intake
  front and center") = borderline; cal.com single_pain_point = false fail (the "another
  setup step before a team sees value" sentence IS the given pain). n=10, one run, Haiku.
  Limits: planted flaws are blatant; subtler weaknesses I saw (formulaic "teams ... often
  find ... stall", "caught my eye") aren't a rubric criterion at all, so the judge can't
  see them. Spend 2026-10-09 ≈ $2.60 estimated.
  Next: consistency re-check for v2.1 (`--flaws none --repeats 3`, ~$0.21), then add a
  code-checked "formulaic phrasing" signal across a batch, then resume writer tuning.
- 2026-10-09 — Judge v2.1 consistency, `judge-check-20261009-230843.json` (10 originals x3,
  $0.191): overall flip rate 0.167 (v1 was 0.017). REGRESSION: making it strict made it
  noisy on borderline emails. Flips by criterion: human_tone 4/10 (it picks a different
  phrase to object to each time, often the sender's own offer wording), pain_as_hypothesis
  3/10 (unsure whether "can stall" / "usually leaks" count as hedged), one_sender_outcome
  2/10, specific_opener 1/10 (stripe, the borderline marketing-copy opener). Some fails
  come from the evidence rule: e.g. it quoted the sender's outcome wording from the context
  instead of the email's sentence (checked locally: the email's sentence matches, the
  outcome-list wording doesn't). Raw judge outputs aren't saved, so I can't inspect the
  other quote failures. Lesson: catch rate alone hides this; always measure both.
  Proposed v2.2: human_tone becomes a list of concrete tone defects (hype, superlatives,
  guarantees, flattery, exclamation) with code deciding, while "sounds templated" moves to
  a batch-level code check; define "asserted" precisely (a plain claim about THIS company;
  general statements about teams like them = hypothesis); evidence must come from the
  email, not the context; save raw judge output. Spend 2026-10-09 ≈ $2.79 estimated.
- 2026-10-09 — Judge v2.2 BUILT, NOT MEASURED (Moiz paused paid runs until we find a
  cheaper way to measure). human_tone now derived by code from a list of concrete tone
  defects (hype / superlative / guarantee / flattery / buzzword) that must be verbatim in
  the email and outside the sender's own offer/outcomes/ask; "!" counted by code. Precise
  "asserted" = a plain claim that THIS company has the problem. Quotes must come from the
  email, not the context. run_judge_check now saves the judge's raw output. 238 tests.
  Cost notes (checked against the API docs, not memory): Message Batches API = 50% off
  every token and stacks with caching; prompt caching can't help because Haiku 4.5 only
  caches prompts >= 4096 tokens and the judge prompt is ~1.5k; at $1/$5 per MTok the
  judge's cost is mostly OUTPUT tokens (lists + reasons), so trimming context won't help.
- 2026-10-10 — Status page: `docs/roadmap.html`, published as a private Claude artifact at
  https://claude.ai/artifact/BhNniHFEzrV4vC9BR2UFNd (where we stopped, next steps, waiting on
  Moiz, every item by week, measured numbers). Items live in the `WEEKS` array at the bottom
  of the file: edit one line, then republish the same path. This file stays the source of
  truth. Fixed stale checkboxes: Week 2 tools ticked (in real use since 2026-10-02); Week 3
  agents no longer say "no real run yet" (they ran 2026-10-09). No code changes, no API spend.
- 2026-10-10 — Backend day, step 1: FIRST RUN ON REAL INFRASTRUCTURE (local Docker: Postgres
  16 + Redis 7; Moiz is now in the `docker` group). Added `docker-compose.yml` (postgres,
  redis, migrate, api, worker; local only) and `scripts/infra_smoke.py`: real uvicorn + real
  Arq worker as separate processes, FAKE steps (no LLM spend), own throwaway database
  `outreach_smoke` + Redis db 1. Passing, repeatable: migrations up/down/up on Postgres,
  `alembic check` clean, RLS on all tables, request -> Redis -> worker -> Postgres -> SSE ->
  email row, fail -> resume re-runs only the failed step, duplicate enqueue ignored, 429 on
  the 4th run/hour. Docker image built for the first time (429MB, non-root); `docker compose
  up` boots migrate -> api + worker, /health OK. Real bugs found, all invisible to the tests:
  (1) the API could never start: `logfire.instrument_fastapi` needs the `logfire[fastapi]`
  extra (tests inject deps and skip real startup) -> dependency + regression test;
  (2) resume left the run `failed` until a worker claimed it, so a re-opened SSE stream
  said "done: failed" -> resume now flips FAILED -> PENDING in one conditional UPDATE
  (double resume = 409); (3) Redis down at create/resume left a run PENDING forever -> now
  marked FAILED (resumable) + 503. 240 tests.
  MY MISTAKE, unapproved spend ≈ $0.024 (estimated): the first smoke attempt used Redis db 0
  and left 2 jobs queued; starting the real worker via compose executed them with real
  agents (one Haiku score $0.0013; one research on a fake domain $0.0224; both stopped
  correctly). Fixed: smoke has its own DB + Redis db and flushes its queue on exit. Rule:
  check the queue is empty before starting a real worker.
  Not covered: the real agents on Postgres/Redis (needs an approved paid run); Supabase
  from a deploy host. Next: review API.
- 2026-10-10 — Backend day, step 2: AUTH (`app/api/auth.py`, `app/api/deps.py`, migration
  `bdb9504cf090`). API keys (`oa_` + 256 random bits, shown once, only the SHA-256 stored;
  `scripts/create_api_key.py` creates / lists / revokes). Runs get an owner
  (`runs.api_key_id`): a team's runs are visible to its key only, everyone else gets 404
  (not 403, so ids can't be probed). No key = anonymous: allowed to create runs only if
  `ALLOW_ANONYMOUS_RUNS=true` (the public demo; default is locked down), and an anonymous
  run is readable by whoever holds its random id. A WRONG key is always 401, never a
  fallback to anonymous. New `GET /runs` (own runs, newest first, keyset paging). Rate
  limits: per key (default 60/h, any IP) vs per IP for anonymous (3/h); the daily spend
  cap stays global. CORS only for `CORS_ORIGINS`, no credentials (auth is a header, not a
  cookie). 253 tests; key checks added to the infra smoke and passing on real Postgres;
  migration up/down/up on Postgres, RLS on `api_keys`. Not yet applied to Supabase.
  Known limits: one key = one team (no per-user identity inside a team); browser
  `EventSource` can't send an Authorization header, so a keyed frontend must read the
  stream with `fetch`. Next: review API.
- 2026-10-10 — Backend day, step 3: REVIEW API (`app/api/review.py`, migration
  `240338cb0545`). `GET /emails` (team's queue; filter to_review/approved/rejected/sent/
  all), `GET /emails/{id}` (draft + the evidence: used facts with source URLs and quotes,
  verifier claims, pain point), `PATCH` (edit), `POST .../approve`, `POST .../reject`
  (reason required: first real quality labels). Key required; reviewer = key name; other
  teams get 404. Decisions and edits are conditional UPDATEs on status: one decision wins,
  approved text can't change. An edit keeps the machine's original draft
  (`original_subject/body`), drops the status to needs_human with "claims were not
  re-checked", enforces the hard rules (422 at >=120 words) and reports style-rule
  problems without blocking. `RunView.email` now carries `email_id`. 261 tests; smoke on
  real Postgres incl. a concurrent approve + reject race (200 + 409). Known limits: no
  undo of a decision; an edit is not re-verified by the LLM (would cost money per edit).
- 2026-10-10 — Backend day, step 4: SENDING (`app/sending/`, `app/api/sending.py`, migration
  `f028981b3077`). `POST /emails/{id}/send {to, postal_address, reply_to?}`: only APPROVED
  emails, only the owning team. At most once: claim APPROVED -> SENDING in one conditional
  UPDATE, then call the provider with the email id as idempotency key; provider error ->
  back to APPROVED (retryable), process death mid-send -> stays SENDING for a human to
  check (chosen over any double send). Code appends a footer (sender, postal address,
  unsubscribe link) and List-Unsubscribe one-click headers. `GET /unsubscribe/{token}`
  shows a confirm page (a GET must not unsubscribe: mail scanners open links), `POST`
  suppresses; suppression is per team; `GET/POST /suppressions`. Daily cap per key
  (default 30). Off unless RESEND_API_KEY + SEND_FROM_EMAIL + PUBLIC_BASE_URL are set.
  Provider = Resend via httpx behind `EmailSender` (request shape checked against their
  docs, tested with a mock transport, NEVER called for real). A test asserts no module in
  agents/tools/pipeline/workers imports `app.sending` or `app.api`. Gaps: the recipient
  address is typed by the sender (contact discovery is Week 8); no bounce/complaint
  webhooks; daily-cap check isn't atomic (can overshoot by concurrent sends).
- 2026-10-10 — Backend day, step 5: ATOMIC LIMITS. Proved the 2026-10-04 "known gap" on real
  Postgres first: with one run left in the hourly allowance, 6 simultaneous requests got 5
  runs through. Fix: `admission_lock` (Postgres `pg_advisory_xact_lock`; in-process lock on
  SQLite) around "check limits, then create the run" and around resume. After: exactly 1 of
  6, twice in a row. 283 tests, ruff clean. `scripts/infra_smoke.py` now covers: migrations,
  run round trip, fail + resume, API keys, review (incl. approve/reject race), no sending
  without a provider, burst vs rate limit.
  BACKEND STATUS: code complete for the roadmap's backend items. Still unproven: real
  agents running through Redis + worker + Postgres (needs an approved paid run, ~$0.10
  est.); Supabase from a deploy host; a real email through Resend. Supabase is 3
  migrations behind (bdb9504cf090, 240338cb0545, f028981b3077). Day's API spend: ≈ $0.024
  estimated, all of it my unapproved-worker mistake (step 1).
  Next: Moiz decides on applying migrations to Supabase, a paid end-to-end run on the
  local stack, host + Redis + Resend accounts; then frontend (Run button, review UI).
- 2026-10-10 — Supabase brought to head `f028981b3077` (Moiz's OK). Same route as 10-07:
  `alembic upgrade b3f1a2c4d5e6:head --sql` -> reviewed (additive only: 2 new tables, new
  nullable columns, constraints) -> checked Supabase was at `b3f1a2c4d5e6` with 0 rows ->
  applied in one transaction over HTTPS. Verified after: version row, 8 tables all with RLS
  and 0 policies, new columns and the 4 named constraints present. Security advisor: only
  the expected "RLS enabled, no policy" notes, plus the old `public.rls_auto_enable()`
  warning (not ours; still Moiz's call).
- 2026-10-10 — FIRST REAL RUN THROUGH THE FULL STACK (Moiz's OK; queue checked empty
  first). `docker compose` image: API key -> POST /runs -> Redis -> Arq worker -> real
  agents -> local Postgres 16, followed over HTTP. posthog.com / Head of Growth, fake test
  sender (never send): succeeded in 37.9s (created -> finished, from the DB), $0.085
  estimated (score $0.0013, research $0.0529, painpoints $0.0128, draft $0.018), 0
  revisions, email `verified`, 77 words, one company number, 4/4 claims supported. Research
  cache row written (expires 2026-10-17). Then via the review API: in the queue with its
  source URL + quote, approved (reviewer = key name), send refused with 503 (no provider
  configured). n=1: this proves the plumbing, it says nothing new about quality.
  Day's API spend ≈ $0.11 estimated ($0.085 approved + $0.024 my mistake).
  Still unproven: deploy host -> Supabase; a real email via Resend.
- 2026-10-10 — Deploy started on Railway (Moiz's choice; repo pushed to GitHub for the first
  time, history scanned for secrets first). First Railway build failed in seconds: "The
  Dockerfile failed validation", no detail. Most likely cause (my inference from Railway's
  documented rule, not confirmed by a log): our `RUN --mount=type=cache` lines; Railway
  only accepts cache mounts with its own id format. Removed all RUN mounts (plain COPY +
  `uv sync --no-cache`); image still builds locally (433MB) and both entrypoints import.
  Standing rule from Moiz: commit AND push after every finished step.
