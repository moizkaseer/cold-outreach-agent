"""fetch_page tests. Hosts are public IP literals so no real DNS or network is used."""

import httpx
import pytest

from app.tools.fetch import FetchError, _assert_public_url, fetch_page, make_client
from app.tools.text import normalise, quote_in_text

PUBLIC = "http://93.184.216.34"  # a global IP literal: passes the SSRF check, no DNS

HTML = """<html><head><title>Acme Widgets</title></head><body>
<nav>Home | Pricing | Login</nav>
<article><h1>About Acme</h1>
<p>Acme was founded in 2019 and now serves over 4,000 customers across Europe.</p>
<p>We’re hiring engineers in Berlin and Lisbon to build our next platform.</p>
</article><footer>&copy; Acme</footer></body></html>"""


def client_for(handler) -> httpx.AsyncClient:
    return make_client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/admin",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://[::1]/",
        "ftp://93.184.216.34/file",
        "file:///etc/passwd",
    ],
)
async def test_blocks_non_public_urls(url: str) -> None:
    with pytest.raises(FetchError):
        await _assert_public_url(url)


async def test_extracts_main_text() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=HTML)

    async with client_for(handler) as c:
        page = await fetch_page(f"{PUBLIC}/about", client=c)
    assert page.title  # trafilatura prefers the <h1> ("About Acme") over <title>
    assert "4,000 customers" in page.text
    assert not page.truncated


async def test_redirect_to_private_address_is_blocked() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "93.184.216.34":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        raise AssertionError("private address must never be requested")

    async with client_for(handler) as c:
        with pytest.raises(FetchError, match="non-public"):
            await fetch_page(f"{PUBLIC}/go", client=c)


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(404), "HTTP 404"),
        (
            httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}),
            "content-type",
        ),
        (httpx.Response(200, html="<html><body></body></html>"), "no main text"),
    ],
)
async def test_fetch_errors(response: httpx.Response, match: str) -> None:
    async with client_for(lambda req: response) as c:
        with pytest.raises(FetchError, match=match):
            await fetch_page(f"{PUBLIC}/x", client=c)


async def test_truncation() -> None:
    async with client_for(lambda req: httpx.Response(200, html=HTML)) as c:
        page = await fetch_page(f"{PUBLIC}/about", client=c, max_chars=20)
    assert page.truncated and len(page.text) == 20


# --- quote matching ---


def test_quote_matching_tolerates_formatting_only() -> None:
    page = "We’re hiring engineers\nin  Berlin — and Lisbon."
    assert quote_in_text("we're hiring engineers in Berlin - and Lisbon", page)
    assert not quote_in_text("We are hiring engineers in Berlin", page)  # paraphrase
    assert not quote_in_text("   ", page)
    assert normalise("A B") == "a b"
