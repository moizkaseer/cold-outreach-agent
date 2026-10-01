"""Fetch a web page and extract its main text (httpx + trafilatura).

Everything returned from here is UNTRUSTED data: it came from the open web and may
contain prompt-injection attempts. Callers must treat it as content, never as
instructions.

Safety: the research agent picks URLs itself, and those URLs can be steered by injected
page text. So we refuse anything that isn't public http(s), including on every redirect
hop — otherwise a page could make our server fetch internal addresses (SSRF), e.g. a cloud
metadata endpoint.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
import trafilatura

USER_AGENT = "OutreachResearchBot/0.1 (+https://moizali.vercel.app)"
TIMEOUT_S = 15.0
MAX_BYTES = 5_000_000  # raw HTML cap: stop downloading huge pages
DEFAULT_MAX_CHARS = 50_000  # extracted text cap
ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class FetchError(Exception):
    """Fetch failed in an expected way (blocked URL, HTTP error, no text, ...)."""


@dataclass(frozen=True)
class PageText:
    url: str  # what was requested
    final_url: str  # after redirects
    title: str | None
    text: str
    truncated: bool


async def _assert_public_url(url: httpx.URL | str) -> None:
    parts = urlsplit(str(url))
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"blocked scheme: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise FetchError("URL has no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchError(f"DNS lookup failed for {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:  # private, loopback, link-local, reserved, ...
            raise FetchError(f"blocked non-public address for {host}: {ip}")


async def _check_request(request: httpx.Request) -> None:
    # httpx calls this for the first request AND every redirect hop.
    await _assert_public_url(request.url)


def make_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """`transport` is for tests (httpx.MockTransport); the URL safety hook still runs."""
    return httpx.AsyncClient(
        transport=transport,
        follow_redirects=True,
        max_redirects=5,
        timeout=TIMEOUT_S,
        headers={"User-Agent": USER_AGENT},
        event_hooks={"request": [_check_request]},
    )


def _extract(html: str, url: str) -> tuple[str | None, str | None]:
    doc = trafilatura.bare_extraction(
        html, url=url, with_metadata=True, include_comments=False, include_tables=True
    )
    if doc is None:
        return None, None
    return doc.title, doc.text


async def fetch_page(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> PageText:
    own_client = client is None
    client = client or make_client()
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code}")
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype and ctype not in ALLOWED_TYPES:
                raise FetchError(f"unsupported content-type: {ctype}")
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    break  # keep what we have; main content is usually early
            final_url = str(resp.url)
            html = body.decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as e:
        raise FetchError(f"{type(e).__name__}: {e}") from e
    finally:
        if own_client:
            await client.aclose()

    if ctype == "text/plain":
        title, text = None, html
    else:
        # trafilatura is CPU-bound and sync: run it off the event loop.
        title, text = await asyncio.to_thread(_extract, html, final_url)
    if not text or not text.strip():
        raise FetchError("no main text could be extracted")

    truncated = len(text) > max_chars
    return PageText(url, final_url, title, text[:max_chars], truncated)
