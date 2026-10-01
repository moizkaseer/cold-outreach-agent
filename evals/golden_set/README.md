# Golden set

One JSON file per company (`<domain>.json`). Evals score only facts with `verified: true`.

## How to verify a fact

1. Run `uv run python -m scripts.check_golden <domain>` — confirms each `evidence` quote
   is still on its `source_url` (the mechanical half).
2. Open the `source_url` and read the quote in context. Ask:
   - Is the `claim` a fair reading of the quote — no stronger, no broader?
   - Is it the company's own current statement (not an old press snippet or a stale bio)?
3. If yes, set `"verified": true` and `"verified_on": "YYYY-MM-DD"`. If not, fix or delete it.

`size` and `segment` are rough labels to keep the set varied; they are not scored.

## Known hard cases (keep these — they test the research agent)

- **deel.com** — the page contradicts itself: the headline says "40,000+ companies", but a
  founder bio further down says "35,000+ customers". Only the headline figure is in the set.
- **wise.com** — the About page embeds old third-party press snippets (valuation, IPO
  talk). These are excluded: not the company's own current claim.
- **gitlab.com** — the "by the numbers" figures are rendered by JavaScript and are missing
  from fetched HTML. Only text facts are used.
- **Bot-blocked / moved pages** seen while drafting: gymshark.com, warbyparker.com,
  canva.com, chime.com, gusto.com (HTTP 403); ramp.com/about, airtable.com/company (404);
  intercom.com/about redirects to fin.ai.
