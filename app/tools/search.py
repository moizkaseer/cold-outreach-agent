"""Web search behind a small interface, so providers can be swapped (Exa now).

Search results are for DISCOVERY only: they tell the research agent which pages to fetch.
Facts must be cited from pages the agent fetched itself (`fetch_page`), because only then
can we check the evidence quote against text we actually hold. Snippets are untrusted
web content, like everything else from the open web.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

SNIPPET_CHARS = 500  # enough to judge relevance; full text comes from fetch_page


class SearchError(Exception):
    """Search failed (auth, rate limit, provider down, bad response)."""


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    published_date: str | None
    snippet: str


@dataclass(frozen=True)
class SearchResponse:
    results: list[SearchResult]
    cost_usd: float | None  # provider-reported, for per-run cost tracking


class SearchProvider(Protocol):
    async def search(
        self, query: str, *, num_results: int = 5, include_domains: list[str] | None = None
    ) -> SearchResponse: ...


class ExaSearch:
    """Exa search via its REST API (POST /search). Docs: https://exa.ai/docs/reference/search"""

    BASE_URL = "https://api.exa.ai"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._headers = {"x-api-key": api_key, "content-type": "application/json"}

    async def search(
        self, query: str, *, num_results: int = 5, include_domains: list[str] | None = None
    ) -> SearchResponse:
        body: dict = {
            "query": query,
            "numResults": num_results,
            "type": "auto",
            "contents": {"text": {"maxCharacters": SNIPPET_CHARS}},
        }
        if include_domains:
            body["includeDomains"] = include_domains
        try:
            resp = await self._client.post(
                f"{self.BASE_URL}/search", json=body, headers=self._headers
            )
        except httpx.HTTPError as e:
            raise SearchError(f"{type(e).__name__}: {e}") from e

        if resp.status_code in (401, 403):
            raise SearchError("search auth failed: check EXA_API_KEY")
        if resp.status_code == 429:
            raise SearchError("search rate-limited; try again shortly")
        if resp.status_code >= 400:
            raise SearchError(f"search HTTP {resp.status_code}")

        try:
            data = resp.json()
            results = [
                SearchResult(
                    title=r.get("title") or "",
                    url=r["url"],
                    published_date=r.get("publishedDate"),
                    snippet=(r.get("text") or "")[:SNIPPET_CHARS],
                )
                for r in data.get("results", [])
            ]
        except (ValueError, KeyError, TypeError) as e:
            raise SearchError(f"unexpected search response: {e}") from e
        cost = (data.get("costDollars") or {}).get("total")
        return SearchResponse(results, float(cost) if cost is not None else None)

    async def aclose(self) -> None:
        await self._client.aclose()
