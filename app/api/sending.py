"""Sending, unsubscribe and the suppression list.

    POST /emails/{id}/send        send an APPROVED email to the address given
    GET  /unsubscribe/{token}     confirmation page (the link in every email)
    POST /unsubscribe/{token}     unsubscribe (also the one-click target for mail clients)
    GET  /suppressions            addresses this team must never email
    POST /suppressions            add one by hand (e.g. someone replied "stop")

Rules enforced here, in code:
- Only an email a human APPROVED can be sent, and only by the team that owns it.
- At most once: the email is claimed (APPROVED -> SENDING) in one conditional UPDATE
  before the provider is called, and the provider gets the email id as an idempotency
  key. If the process dies mid-send the email stays SENDING and needs a person to look;
  that is chosen over any chance of sending twice.
- Every email carries the sender's postal address and a working unsubscribe link
  (CAN-SPAM), and unsubscribed addresses are refused for that team from then on.
- A daily cap per team keeps volume low.

The recipient address comes from the person sending (contact discovery is a later
step). Nothing here is reachable by any agent.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import Team, get_deps
from app.api.review import EmailDetail, _detail, _owned
from app.db.models import EmailDraft, EmailStatus, Run, Suppression, utcnow
from app.schemas import Sender
from app.sending import OutboundEmail, SendError

router = APIRouter(tags=["sending"])

Address = Annotated[EmailStr, Field(max_length=320)]


class SendEmail(BaseModel):
    to: Address
    # CAN-SPAM requires a valid physical postal address in commercial email.
    postal_address: str = Field(min_length=10, max_length=200)
    reply_to: Address | None = None


class AddSuppression(BaseModel):
    email: Address


class SuppressionView(BaseModel):
    email: str
    created_at: dt.datetime


def unsubscribe_url(base: str, token: str) -> str:
    return f"{base.rstrip('/')}/unsubscribe/{token}"


def build_message(email: EmailDraft, sender: Sender, body: SendEmail, link: str) -> OutboundEmail:
    address = " ".join(body.postal_address.split())  # one line, no smuggled line breaks
    footer = f"--\n{sender.name}, {sender.company}\n{address}\nUnsubscribe: {link}"
    return OutboundEmail(
        from_name=sender.name,
        to=body.to.lower(),
        subject=email.subject,
        text=f"{email.body.rstrip()}\n\n{footer}\n",
        reply_to=body.reply_to,
        # Lets mail clients show their own "unsubscribe" button (RFC 8058 one-click).
        headers={
            "List-Unsubscribe": f"<{link}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },  # fmt: skip
    )


async def suppress(sessions: async_sessionmaker[AsyncSession], api_key_id: uuid.UUID,
                   address: str) -> None:  # fmt: skip
    """Idempotent: suppressing an address twice is not an error."""
    async with sessions() as s:
        s.add(Suppression(api_key_id=api_key_id, email=address.lower()))
        try:
            await s.commit()
        except IntegrityError:  # already on the list
            await s.rollback()


@router.post("/emails/{email_id}/send")
async def send_email(
    email_id: uuid.UUID, body: SendEmail, request: Request, team: Team
) -> EmailDetail:
    d = get_deps(request)
    if d.mailer is None or not d.public_base_url:
        raise HTTPException(503, "sending is not configured on this server")
    to = body.to.lower()
    token = secrets.token_urlsafe(24)

    async with d.sessions() as s:
        email = await _owned(s, email_id, team)
        if email.status is not EmailStatus.APPROVED:
            raise HTTPException(
                409, f"only approved emails can be sent (status: {email.status.value})"
            )
        if "\n" in email.subject or "\r" in email.subject:
            raise HTTPException(422, "subject must be a single line")
        if await s.scalar(
            select(Suppression.id).where(Suppression.api_key_id == team.id, Suppression.email == to)
        ):
            raise HTTPException(409, "this address unsubscribed; it can't be emailed")
        if d.sends_per_key_per_day:
            midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            today = await s.scalar(
                select(func.count(EmailDraft.id))
                .join(Run, EmailDraft.run_id == Run.id)
                .where(Run.api_key_id == team.id, EmailDraft.sent_at >= midnight)
            )
            if (today or 0) >= d.sends_per_key_per_day:
                raise HTTPException(429, f"daily sending cap reached ({d.sends_per_key_per_day})")

        claimed = await s.execute(
            update(EmailDraft)
            .where(EmailDraft.id == email_id, EmailDraft.status == EmailStatus.APPROVED)
            .values(status=EmailStatus.SENDING, sent_to=to, sent_at=utcnow(),
                    unsubscribe_token=token)
        )  # fmt: skip
        await s.commit()
        if claimed.rowcount != 1:
            raise HTTPException(409, "this email is already being sent")
        msg = build_message(email, Sender.model_validate(email.run.sender), body,
                            unsubscribe_url(d.public_base_url, token))  # fmt: skip

    try:
        message_id = await d.mailer.send(msg, idempotency_key=str(email_id))
    except SendError as e:
        # Not accepted, so release the claim and let the reviewer retry. A retry reuses
        # the same idempotency key, so even a request that timed out after the provider
        # took it can't turn into a second delivery (provider keeps keys for 24h).
        async with d.sessions() as s:
            await s.execute(
                update(EmailDraft)
                .where(EmailDraft.id == email_id, EmailDraft.status == EmailStatus.SENDING)
                .values(status=EmailStatus.APPROVED, sent_to=None, sent_at=None,
                        unsubscribe_token=None)
            )  # fmt: skip
            await s.commit()
        raise HTTPException(502, f"not sent: {e}") from e

    async with d.sessions() as s:
        await s.execute(
            update(EmailDraft)
            .where(EmailDraft.id == email_id)
            .values(status=EmailStatus.SENT, provider_message_id=message_id)
        )
        await s.commit()
        return await _detail(s, await _owned(s, email_id, team))


@router.get("/suppressions")
async def list_suppressions(request: Request, team: Team) -> list[SuppressionView]:
    async with get_deps(request).sessions() as s:
        rows = await s.scalars(
            select(Suppression)
            .where(Suppression.api_key_id == team.id)
            .order_by(Suppression.created_at.desc())
        )
        return [SuppressionView(email=r.email, created_at=r.created_at) for r in rows]


@router.post("/suppressions", status_code=201)
async def add_suppression(body: AddSuppression, request: Request, team: Team) -> dict[str, str]:
    await suppress(get_deps(request).sessions, team.id, body.email)
    return {"email": body.email.lower(), "status": "suppressed"}


# --- unsubscribe: public, the token in the link is the only credential ---

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Unsubscribe</title>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
{content}</body></html>"""
# A GET must not unsubscribe by itself: mail scanners open every link in an email.
CONFIRM = """<h1>Unsubscribe?</h1><p>You won't get further emails from this sender.</p>
<form method="post"><button type="submit" style="font-size:1rem;padding:.5rem 1rem">
Unsubscribe</button></form>"""
DONE = "<h1>You're unsubscribed</h1><p>This sender won't email you again.</p>"


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe_page(token: str) -> str:
    return PAGE.format(content=CONFIRM)


@router.post("/unsubscribe/{token}", response_class=HTMLResponse)
async def unsubscribe(token: str, request: Request) -> str:
    sessions = get_deps(request).sessions
    async with sessions() as s:
        row = (
            await s.execute(
                select(EmailDraft.sent_to, Run.api_key_id)
                .join(Run, EmailDraft.run_id == Run.id)
                .where(EmailDraft.unsubscribe_token == token)
            )
        ).one_or_none()
    if row is not None and row.sent_to and row.api_key_id:
        await suppress(sessions, row.api_key_id, row.sent_to)
    # Same answer for unknown tokens: the page must not confirm which tokens are real.
    return PAGE.format(content=DONE)
