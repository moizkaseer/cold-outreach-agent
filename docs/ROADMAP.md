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
