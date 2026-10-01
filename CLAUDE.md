# CLAUDE.md — Outreach Agent

This file is the project's memory. Read it fully before doing anything. Everything here
has already been decided — do not ask me questions this file answers.

## Who I am and how to work with me

I'm Moiz, a final-semester CS student building this as a production-grade portfolio
project (I'm looking to join a serious AI team). I want to **learn**, not just get code.

- Work in small steps. One step = one focused change I can read and run.
- Before writing code for a step, give a short plan (what files, why). Then build it.
- Explain the *why* behind non-obvious decisions, briefly. Teach me as we go.
- After each step: tell me exactly how to run/test it, and update `docs/ROADMAP.md`.
- Only ask me a question if you are truly blocked (e.g. a missing API key or a choice
  with real tradeoffs not covered here). Otherwise make a sensible choice and say so.
- Never invent metrics, results, or facts. If something isn't measured, say so.

## What we're building

A cold-outreach agent. Input: a company website (domain) + a person's name and role.
Output: a short, genuinely personalised cold email where **every fact about the company
is backed by a real source URL**.

It must be production-grade: grounded, evaluated, durable, observable, and guarded.
Not a demo that calls an LLM once and hopes.

## Core principles (non-negotiable)

1. **Code controls the flow; the LLM does the thinking.** The pipeline is a plain Python
   state machine. Claude is only called where judgment is needed. No agent swarms for a
   fixed pipeline.
2. **Grounding.** The research agent must attach a `source_url` to every fact. The
   verifier rejects any email claim not supported by research sources.
3. **Evals before features.** Every agent change is measured against the golden set.
4. **Durability.** Every pipeline step persists its status and output under a `run_id`,
   so a failed run resumes from the failed step, not from scratch.
5. **Observability.** Trace every LLM call and tool call with tokens and cost.
6. **Least privilege.** Fetched web content is untrusted data (prompt injection risk).
   The research agent has NO tools that send email, write to the CRM, or touch the DB.
7. **No over-engineering.** No Kubernetes, Kafka, LangGraph, etc. Add complexity only
   when a real problem demands it.

## Architecture

```
validate → dedupe → score (quality gate) → RESEARCH AGENT (tool loop)
→ pain points → writer → VERIFIER → (revise, max 2x) → human review → send
```

- **Research agent** — the only autonomous agent. Tools: `search`, `fetch_page`,
  `finish`. Hard limits: ~10 turns, token budget per run. Returns a Pydantic
  `ResearchReport`; every fact has `source_url`.
- **Pain point agent** — runs AFTER research (not in parallel), reasons from evidence in
  the report, returns ranked pain points with rationale + supporting fact references.
- **Writer** — email rules: under 120 words; open with one specific, sourced company
  reference; exactly one pain point (top ranked); one concrete outcome; one low-friction
  ask; plain text; no "I hope this finds you well"; never mention AI/automation.
- **Verifier** — checks each factual claim in the email against the report's sources.
  Unsupported → back to writer with feedback (max 2 revisions), then flag for human.
- **Human review** — no email is auto-sent. A person approves every email.

### Agent implementation decision

Build our **own agent loop on the Anthropic Python SDK** first (tool use: call → run
tools → return results → repeat until `finish` or budget hit). Reasons: full control of
cost/retries/limits, lighter to scale in workers, and I learn how agents really work.
Put research behind an interface (`async def research(lead) -> ResearchReport`) so a
Claude Agent SDK (`claude-agent-sdk`) implementation can be added later and compared on
the evals.

## Tech stack

| Layer | Choice |
|---|---|
| Language / packaging | Python 3.12, `uv` |
| API | FastAPI, Server-Sent Events for live progress |
| Background jobs | Redis + Arq (agent runs take 30–120s; never run them in the request) |
| Database | Postgres (Supabase), SQLAlchemy 2.0 async + Alembic migrations |
| LLM | Anthropic Python SDK. Sonnet for research + writing, Haiku for scoring, classification, verification |
| Schemas | Pydantic v2 for every agent input/output |
| Search / fetch | A search API (Exa, Tavily or Brave — pick one, behind an interface) + `httpx` + `trafilatura` |
| Observability | Langfuse or Logfire |
| Tests | pytest, pytest-asyncio |
| Lint / format | ruff |
| Deploy | Docker on Railway/Render/Fly. Frontend (moizali.vercel.app) calls the API |

Model names, API keys and limits live in env vars / `app/config.py` (pydantic-settings),
never hard-coded. Current model IDs: `claude-sonnet-5-5`, `claude-haiku-4-5-20251001`.

## Project structure

```
app/
  api/          # FastAPI routes, SSE streaming
  pipeline/     # orchestrator state machine
  agents/       # research, painpoints, writer, verifier
  tools/        # search, fetch, extract
  schemas/      # Pydantic models
  db/           # models, migrations
  workers/      # Arq jobs
  config.py
evals/
  golden_set/   # 30+ real companies with hand-verified facts
  run_evals.py
tests/
docs/ROADMAP.md
Dockerfile
```

## Conventions

- Type hints everywhere; async I/O.
- Prompts live in their own files/constants next to each agent, not inline in logic.
- Every LLM output is parsed into a Pydantic model; on validation failure, retry once,
  then mark the step failed.
- Cache research per domain (7 days). Use prompt caching for long system prompts.
- Secrets only in `.env` (gitignored). Provide `.env.example`.
- Write tests for tools and pipeline logic; use evals (not unit tests) for LLM quality.

## Things we will NOT do

- Scrape LinkedIn (ToS). Contact discovery later uses providers like Hunter/Apollo.
- Auto-send emails. Sending only after human approval; separate domain with
  SPF/DKIM/DMARC, low volume, unsubscribe link (CAN-SPAM / GDPR).
- Give any agent more tools than it needs.
- Let the public demo run without rate limits and a spending cap.

## Roadmap and current status

@docs/ROADMAP.md
