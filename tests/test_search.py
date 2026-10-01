import json

import httpx
import pytest

from app.tools.search import SNIPPET_CHARS, ExaSearch, SearchError


def exa_with(handler) -> ExaSearch:
    return ExaSearch("test-key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_request_shape_and_parsing() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["key"] = req.headers["x-api-key"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "About Acme",
                        "url": "https://acme.com/about",
                        "publishedDate": "2026-01-02T00:00:00.000Z",
                        "text": "x" * 2000,
                    },
                    {"title": None, "url": "https://acme.com/blog", "text": None},
                ],
                "costDollars": {"total": 0.005},
            },
        )

    resp = await exa_with(handler).search(
        "acme funding", num_results=3, include_domains=["acme.com"]
    )

    assert seen["url"] == "https://api.exa.ai/search"
    assert seen["key"] == "test-key"
    assert seen["body"]["query"] == "acme funding"
    assert seen["body"]["numResults"] == 3
    assert seen["body"]["includeDomains"] == ["acme.com"]
    assert [r.url for r in resp.results] == ["https://acme.com/about", "https://acme.com/blog"]
    assert len(resp.results[0].snippet) == SNIPPET_CHARS
    assert resp.results[1].title == "" and resp.results[1].snippet == ""
    assert resp.cost_usd == 0.005


async def test_no_include_domains_key_when_unset() -> None:
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"results": []})

    resp = await exa_with(handler).search("q")
    assert "includeDomains" not in bodies[0]
    assert resp.results == [] and resp.cost_usd is None


@pytest.mark.parametrize(
    ("status", "match"),
    [(401, "auth"), (429, "rate-limited"), (500, "HTTP 500")],
)
async def test_http_errors(status: int, match: str) -> None:
    with pytest.raises(SearchError, match=match):
        await exa_with(lambda req: httpx.Response(status)).search("q")


async def test_malformed_response() -> None:
    with pytest.raises(SearchError, match="unexpected"):
        await exa_with(
            lambda req: httpx.Response(200, json={"results": [{"title": "no url"}]})
        ).search("q")


async def test_network_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(SearchError, match="ConnectError"):
        await exa_with(handler).search("q")
