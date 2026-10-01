"""Check that every golden-set evidence quote actually appears on its source page.

    uv run python -m scripts.check_golden            # all companies
    uv run python -m scripts.check_golden stripe.com # one company

This automates the mechanical half of verification ("is the quote really there?").
The human half — "is the claim a fair reading of the quote, and still true?" — is yours;
only then set `verified: true`. Exit code 1 if any quote is missing.
"""

import asyncio
import sys

from app.tools.fetch import FetchError, fetch_page, make_client
from app.tools.text import quote_in_text
from evals.golden import load_golden_set


async def main(only: set[str]) -> int:
    companies = [c for c in load_golden_set() if not only or c.domain in only]
    urls = sorted({str(f.source_url) for c in companies for f in c.facts})
    sem = asyncio.Semaphore(8)  # be polite: limited concurrency

    async with make_client() as client:

        async def get(url: str) -> tuple[str, str | Exception]:
            async with sem:
                try:
                    page = await fetch_page(url, client=client, max_chars=10_000_000)
                    return url, page.text
                except FetchError as e:
                    return url, e

        pages = dict(await asyncio.gather(*(get(u) for u in urls)))

    missing = 0
    for c in companies:
        print(f"\n{c.domain}  ({len(c.verified_facts)}/{len(c.facts)} verified)")
        for f in c.facts:
            page = pages[str(f.source_url)]
            if isinstance(page, Exception):
                status, missing = f"FETCH ERROR ({page})", missing + 1
            elif quote_in_text(f.evidence, page):
                status = "ok"
            else:
                status, missing = "QUOTE NOT FOUND", missing + 1
            mark = "x" if f.verified else " "
            print(f"  [{mark}] {status:<16} {f.claim[:70]}")
    print(f"\n{missing} problem(s)")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(set(sys.argv[1:]))))
