<p align="center">
  <img src="docs/assets/banner.png" alt="Outreach Agent: cold emails where every fact has a source" width="100%">
</p>

<p align="center">
  <a href="https://github.com/moizkaseer/cold-outreach-agent/actions/workflows/ci.yml"><img alt="tests" src="https://img.shields.io/github/actions/workflow/status/moizkaseer/cold-outreach-agent/ci.yml?branch=main&style=flat-square&label=tests"></a>
  <img alt="Python 3.12" src="https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white">
  <img alt="Claude" src="https://img.shields.io/badge/Claude-Anthropic_SDK-D97757?style=flat-square">
  <img alt="Postgres" src="https://img.shields.io/badge/Postgres-4169E1?style=flat-square&logo=postgresql&logoColor=white">
  <img alt="Redis" src="https://img.shields.io/badge/Redis-DC382D?style=flat-square&logo=redis&logoColor=white">
  <img alt="No agent framework" src="https://img.shields.io/badge/agent_loop-hand--written-34d399?style=flat-square">
</p>

<p align="center">
  <a href="#the-problem">The problem</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#guarantees-and-where-they-live-in-the-code">Guarantees</a> ·
  <a href="#what-broke-and-what-it-taught-me">What broke</a> ·
  <a href="#numbers-so-far">Numbers</a> ·
  <a href="#run-it">Run it</a>
</p>

<br>

Give it a **company website** and a **person's name and role**. It researches the company,
works out a problem that person probably has, and drafts a short cold email. Then it does
the part most tools skip: it proves every claim, and it refuses to use anything it cannot
prove.

<p align="center">
  <img src="docs/assets/evidence.png" alt="A drafted email to PostHog next to the source URL and exact quote behind each claim" width="100%">
</p>

<p align="center"><sub>A real run on the deployed system. The numbers and the quote are from that run; the sender is a made-up test company.</sub></p>

> [!IMPORTANT]
> Nothing is ever sent automatically. A person approves every email, every email carries
> an unsubscribe link, and an address that opted out can never be emailed again.

## The problem

Sales reps (SDRs) know a researched email beats a template. Research by hand is slow, so
most teams send templates. Asking an LLM to "personalise it" is fast, but it invents
facts, and one made-up funding round in a cold email costs the sender their credibility.

| | Template blast | "Ask an LLM" | Outreach Agent |
|---|:---:|:---:|:---:|
| Specific to this company | No | Yes | Yes |
| Facts come from pages it actually read | n/a | No | **Yes, URL + exact quote** |
| Unsupported claims are caught | n/a | No | **Yes, by code** |
| Sender's results can be invented | No | Yes | **No, only supplied ones** |
| A human approves before sending | Sometimes | Rarely | **Always** |

## How it works

The pipeline is a plain Python state machine. The model is called only where judgement
is needed, and wherever a mistake would matter, **code makes the final decision**.

```mermaid
flowchart LR
    A([validate]) --> B([dedupe]) --> C([score])
    C --> D[["research agent<br/>search · fetch · finish"]]
    D --> E([pain points]) --> F([writer]) --> G{{verifier}}
    G -- "unsupported claim<br/>(max 2 revisions)" --> F
    G --> H[/human review/] --> I([send]) --> J([classify replies])

    classDef agent fill:#0c2a24,stroke:#34d399,color:#d1fae5;
    classDef human fill:#0a2233,stroke:#38bdf8,color:#e0f2fe;
    class D,G agent;
    class H human;
```

<details>
<summary><b>What each step does</b></summary>

<br>

| Step | What it does |
|---|---|
| validate, dedupe | Cleans the input. Skips a person already contacted in the last 30 days. |
| score | A cheap fit check. A poor fit stops here for about $0.001, before research is paid for. |
| research agent | The only autonomous agent. Searches and reads pages, returns facts with a URL and exact quotes. |
| pain points | Reasons from those facts to a likely problem, framed as a hypothesis, citing fact ids. |
| writer | Under 120 words: one sourced opener, one pain point, one proven result, one ask. |
| verifier | Maps each claim in the email to a source. Unsupported claims go back to the writer. |
| human review | Approve, edit or reject, with the evidence next to the draft. |
| send | Through an email provider, at most once, with an unsubscribe link. |
| classify replies | Sorts replies (interested, not now, unsubscribe, ...) and suggests the next step. |

</details>

## Guarantees, and where they live in the code

| Guarantee | How it is enforced | Code |
|---|---|---|
| **No invented company facts** | A fact is kept only if its URL was fetched in this run *and* its quote appears in the page text. The check is code, run after the model finishes. | [`agents/research`](app/agents/research/agent.py) |
| **No unsupported claims in the email** | The verifier maps each claim to a fact and quote. Code decides pass or fail and writes the feedback. | [`agents/verifier`](app/agents/verifier/agent.py), [`drafting.py`](app/agents/drafting.py) |
| **A crash never costs twice** | Every step saves its status, output and cost. A failed run resumes from the failed step. | [`pipeline/orchestrator.py`](app/pipeline/orchestrator.py) |
| **A hostile web page can't act** | Fetched text is wrapped as untrusted data. The research agent has three tools and none can send, write or delete. | [`agents/loop.py`](app/agents/loop.py), [`evals/injection`](evals/injection/cases.py) |
| **An email is sent at most once** | The email is claimed in one conditional `UPDATE` before the provider is called, with an idempotency key. | [`api/sending.py`](app/api/sending.py) |
| **An opt-out always wins** | "Remove me" in a reply is caught by plain code, whatever the model says and even if the model is down. | [`agents/replies/rules.py`](app/agents/replies/rules.py) |
| **Spending is bounded** | Per-team and per-visitor rate limits plus a daily cap, made atomic with a Postgres advisory lock. | [`api/limits.py`](app/api/limits.py) |
| **Teams can't see each other** | API keys are stored as hashes. Another team's data returns 404, not 403. | [`api/deps.py`](app/api/deps.py) |

The agent loop is **written by hand on the Anthropic SDK**, with no agent framework: call
the model, run its tool calls, return the results, repeat until it finishes or hits its
turn or token budget. That keeps cost, retries and limits fully visible.

## What broke, and what it taught me

The useful part of building this was not the happy path. These are real findings from
the [dated log](docs/ROADMAP.md).

<details open>
<summary><b>240 tests passed, and the API could not start</b></summary>

<br>

The tests inject their own dependencies, so they never ran the real startup code. The
first time the server ran as a real process it crashed on a missing package extra. I now
run a free smoke test that boots the real server, a real worker, Postgres and Redis.
*Lesson: tests that skip startup tell you nothing about startup.*

</details>

<details>
<summary><b>The LLM judge said 92% faithful. Reading the facts myself said about 75%</b></summary>

<br>

An early judge passed claims whose dates and numbers were in no quote at all. Changing
only the judge moved the score for the same 24 facts from 0.92 to 0.67, and to 0.42 with
a stronger model. I rebuilt it so the
model lists each detail and names its quote, and code derives the verdict.
*Lesson: an uncalibrated judge is a random number with a confident face.*

</details>

<details>
<summary><b>Making the email judge stricter made it noisier</b></summary>

<br>

To test the judge without expert labels, I planted known flaws in good emails. Version 1
caught 23 of 35. Version 2.1 caught 69 of 70, which looked like a win until I measured
consistency: its flip rate on unedited emails went from 0.017 to 0.167.
*Lesson: catch rate alone hides the damage. Measure both.*

</details>

<details>
<summary><b>A rate limit that let 5 of 6 requests through</b></summary>

<br>

"Check the limit, then create the run" is two steps. With one run left in the allowance,
six simultaneous requests got five runs through. A Postgres advisory lock around the pair
makes it exactly one of six.
*Lesson: a limit that isn't atomic is a suggestion.*

</details>

<details>
<summary><b>Grounding proves a quote is on the page, not that the page is honest</b></summary>

<br>

Prompt-injection test pages showed that injected text can be quoted and pass the
grounding check. The defence is layered instead: untrusted-content wrapping, an agent
with no dangerous tools, and a human reviewer.
*Lesson: know exactly what a check proves, and write down what it doesn't.*

</details>

## Numbers so far

Small samples. Read them as indications, not benchmarks. Costs are estimates from token
counts plus the search provider's reported cost.

| | | |
|---|---|---|
| **$0.08 to $0.14** per lead | **34 to 78 s** per lead | **60 to 70%** of cost is research |
| 10 leads | 10 leads | first 3 leads |
| **10 of 10** emails passed the verifier | **$0.0013** to reject a poor-fit lead | **329** unit tests, no API calls |
| same 10 leads | stopped before research | run on every push |

Not established yet: how real sales people rate the emails, and reply rates.

## Status

| Part | State |
|---|---|
| Research, pain points, writer, verifier | Working, run on real companies |
| API, queue, worker, database, resume | Working, deployed (Railway + Supabase) |
| API keys, per-team data, rate limits, spend cap | Working |
| Review API (approve, edit, reject) | Working, no screen for it yet |
| Sending | Built and tested with a fake provider. No real email sent yet |
| Reply classification | Built and tested with fakes. Accuracy not measured yet |
| Frontend | Not started |

Next: one real email through the provider, measuring the reply classifier, contact
discovery, and a fit signal ("do they already use a competitor?").

## Run it

You need [uv](https://docs.astral.sh/uv/) and Docker. Real runs also need an Anthropic
API key and an [Exa](https://exa.ai) key.

```bash
git clone https://github.com/moizkaseer/cold-outreach-agent.git
cd cold-outreach-agent
uv sync
uv run pytest                 # 329 tests, fakes only, costs nothing
```

<details>
<summary><b>Start the full stack and run a lead</b> (about $0.09 per run)</summary>

<br>

```bash
cp .env.example .env          # add ANTHROPIC_API_KEY and EXA_API_KEY
docker compose up --build     # Postgres, Redis, migrations, API, worker

# in a second terminal: create an API key
DATABASE_URL=postgresql+asyncpg://postgres:outreach@localhost:5432/outreach \
  uv run python -m scripts.create_api_key "My team"

curl -X POST http://localhost:8000/runs \
  -H "Authorization: Bearer YOUR_KEY" -H "Content-Type: application/json" \
  -d '{
    "domain": "posthog.com",
    "person_name": "Alex Morgan",
    "person_role": "Head of Growth",
    "sender": {
      "name": "Your name",
      "company": "Your company",
      "offer": "What you sell, in one sentence.",
      "outcomes": ["A real result you can prove."],
      "ask": "Open to a 15-minute call next week?"
    }
  }'
```

The answer contains a `run_id`. Follow it with `GET /runs/{run_id}`, or watch it live at
`GET /runs/{run_id}/events`. Interactive docs: http://localhost:8000/docs

</details>

<details>
<summary><b>API overview</b></summary>

<br>

| Endpoint | Purpose |
|---|---|
| `POST /runs` | Start a run for one lead |
| `GET /runs/{id}` · `GET /runs/{id}/events` | Status, steps and cost · live progress |
| `POST /runs/{id}/resume` | Retry a failed run from the failed step |
| `GET /emails` · `GET /emails/{id}` | Review queue · a draft with its evidence |
| `PATCH /emails/{id}` · `POST .../approve` · `POST .../reject` | Review decisions |
| `POST /emails/{id}/send` | Send an approved email |
| `POST /emails/{id}/replies` · `GET /replies` | Classify and list replies |
| `GET /unsubscribe/{token}` · `GET /suppressions` | Opt-out handling |

</details>

<details>
<summary><b>Project layout</b></summary>

<br>

```
app/
  agents/      research, pain points, writer, verifier, scorer, replies, and the loop
  pipeline/    the state machine that runs the steps and resumes failures
  api/         FastAPI routes: runs, review, sending, replies, auth, limits
  tools/       search and page fetching
  sending/     email provider behind an interface
  db/          models and migrations
  workers/     the background job
evals/         golden set, fact judge, email judge, prompt-injection cases
scripts/       smoke tests and helpers
tests/         unit tests (fakes only)
docs/          roadmap and dated progress log
```

</details>

## Tech

Python 3.12 · FastAPI with Server-Sent Events · Redis + Arq · Postgres, SQLAlchemy 2.0
async, Alembic · Pydantic v2 · Anthropic Python SDK (Sonnet for research and writing,
Haiku for scoring, verification and classification) · Exa search · Logfire tracing ·
Docker · Railway · Supabase

## It will not

Scrape LinkedIn · send an email no human approved · email an address that unsubscribed ·
give any agent a tool it does not need.

---

<p align="center">
  Built by <a href="https://github.com/moizkaseer">Moiz Ali</a>, a final-semester computer science student,<br>
  to learn how an LLM application is built when it has to be right: grounded, evaluated, durable, observable and guarded.
</p>
