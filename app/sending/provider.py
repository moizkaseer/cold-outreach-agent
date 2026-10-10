"""Email provider behind an interface, so tests use a fake and the provider can change.

`ResendSender` talks to Resend's REST API with httpx (checked against
https://resend.com/docs/api-reference/emails/send-email on 2026-10-10): POST /emails with
from / to / subject / text / reply_to / headers, `Idempotency-Key` header (valid 24h),
response `{"id": ...}`. NOT yet run against the real service: that needs an account and
a sending domain with SPF/DKIM/DMARC set up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

RESEND_URL = "https://api.resend.com/emails"


@dataclass(frozen=True)
class OutboundEmail:
    from_name: str
    to: str
    subject: str
    text: str  # plain text only, by design
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


class SendError(Exception):
    """The provider did not accept the email (or couldn't be reached)."""


class EmailSender(Protocol):
    async def send(self, msg: OutboundEmail, *, idempotency_key: str) -> str:
        """Hand one email to the provider and return its message id. Raises SendError.
        The same idempotency_key must never produce a second delivery."""
        ...


def display_name(name: str) -> str:
    # The name sits inside a From header; drop anything that could break out of it.
    return re.sub(r'[<>"\r\n,;]', "", name).strip() or "Sales"


class ResendSender:
    def __init__(self, api_key: str, from_email: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._from_email = from_email
        self._client = client

    async def send(self, msg: OutboundEmail, *, idempotency_key: str) -> str:
        payload: dict[str, object] = {
            "from": f"{display_name(msg.from_name)} <{self._from_email}>",
            "to": [msg.to],
            "subject": msg.subject,
            "text": msg.text,
            "headers": msg.headers,
        }
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        try:
            r = await self._client.post(
                RESEND_URL,
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Idempotency-Key": idempotency_key},
                timeout=20,
            )  # fmt: skip
        except httpx.HTTPError as e:
            raise SendError(f"could not reach the email provider: {type(e).__name__}") from e
        if r.status_code >= 300:
            raise SendError(f"provider answered {r.status_code}: {r.text[:200]}")
        try:
            return str(r.json()["id"])
        except (ValueError, KeyError, TypeError) as e:
            raise SendError("provider accepted the email but returned no message id") from e
