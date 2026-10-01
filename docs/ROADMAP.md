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
- [ ] Research agent returns cited `ResearchReport`
- [ ] `evals/run_evals.py`: fact accuracy + citation validity vs golden set

## Week 3 — Rest of the chain
- [ ] Pain point agent (sequential, evidence-based)
- [ ] Writer agent (email rules from CLAUDE.md)
- [ ] Verifier + revise loop (max 2)
- [ ] LLM-as-judge rubric for email quality in evals

## Week 4 — Backend
- [ ] Postgres models: runs, steps, leads, research cache, emails (+ Alembic)
- [ ] Orchestrator state machine with resume-from-failed-step
- [ ] Redis + Arq worker
- [ ] FastAPI: create run, get run, SSE progress stream

## Week 5 — Production hardening
- [ ] Tracing (Langfuse or Logfire) with cost per run
- [ ] Per-domain research cache, prompt caching, model routing
- [ ] Prompt-injection test pages in evals

## Week 6 — Ship the demo
- [ ] Connect "Run" button on moizali.vercel.app
- [ ] Rate limiting + daily spending cap
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
