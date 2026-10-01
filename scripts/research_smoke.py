"""Run the research agent once for real and print what it found.

    uv run python -m scripts.research_smoke posthog.com "Jane Doe" "Head of Growth"

Needs ANTHROPIC_API_KEY and EXA_API_KEY in .env. Expect roughly 30-120s and a few cents.
"""

import asyncio
import sys

from app.agents.research import ResearchAgent
from app.config import get_settings
from app.schemas import Lead


async def main(domain: str, name: str, role: str) -> None:
    agent = ResearchAgent.from_settings(get_settings())
    run = await agent.run(Lead(domain=domain, person_name=name, person_role=role))
    a = run.agent

    print("\n--- tool calls ---")
    for c in a.tool_calls:
        arg = c.input.get("query") or c.input.get("url") or ""
        print(
            f"turn {c.turn:>2} {c.name:<10} {arg[:80]}{'  [ERROR] ' + c.output[:80] if c.is_error else ''}"
        )

    print(f"\nstop={a.stop_reason} turns={a.turns}")
    print(
        f"tokens: in={a.usage.input_tokens} out={a.usage.output_tokens} "
        f"cache_read={a.usage.cache_read_input_tokens} "
        f"cache_write={a.usage.cache_creation_input_tokens} | "
        f"search cost ${run.search_cost_usd:.4f}"
    )

    if run.report:
        print(f"\n--- {run.report.company_name}: {len(run.report.facts)} grounded facts ---")
        print(run.report.summary)
        for f in run.report.facts:
            print(f'\n[{f.id}] ({f.category}) {f.claim}\n     {f.source_url}\n     "{f.evidence}"')
    for r in run.rejected:
        print(f"\nREJECTED [{r.fact.id}] {r.fact.claim}\n     reason: {r.reason}")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    asyncio.run(main(*sys.argv[1:]))
