"""Replies to sent emails: classify, store, and honour opt-outs.

    POST /emails/{id}/replies     paste a prospect's reply; it is classified and stored
    GET  /emails/{id}/replies     the replies to one email
    GET  /replies                 this team's replies, newest first (?category=interested)

How it works:
- Only the owning team, and only for an email that was actually SENT.
- The reply text is outside content. A model (no tools) suggests a category; code checks
  it (app/agents/replies). Nothing in a reply can make this endpoint do anything except
  the one side effect below.
- An `unsubscribe` reply puts the address the email was sent to on the team's
  suppression list, before the reply is stored. This also happens when the model is
  down, because the opt-out rule is plain code.
- Classification runs inside the request: it is one short call, unlike a pipeline run.
  Its cost is stored on the reply. A daily cap per team bounds it.

Replies are pasted by a person for now; an inbound-mail webhook can call the same code.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import Team, get_deps
from app.api.review import _owned
from app.api.sending import suppress
from app.db.models import EmailDraft, EmailStatus, Reply, Run, utcnow
from app.pricing import estimate_cost_usd
from app.schemas import ReplyCategory

router = APIRouter(tags=["replies"])

# What the sales person should do. Decided by code from the category, so a reply can't
# talk the model into inventing an action.
NEXT_STEP: dict[ReplyCategory, str] = {
    ReplyCategory.INTERESTED: "Reply yourself and book the call.",
    ReplyCategory.QUESTION: "Answer the question yourself.",
    ReplyCategory.NOT_NOW: "Note the timing and follow up later.",
    ReplyCategory.NOT_INTERESTED: "Close the lead. Do not follow up.",
    ReplyCategory.WRONG_PERSON: "Check whether they named someone else, then start a new lead.",
    ReplyCategory.UNSUBSCRIBE: "Nothing. The address is on your suppression list.",
    ReplyCategory.OUT_OF_OFFICE: "Wait until they are back.",
    ReplyCategory.OTHER: "Read the reply yourself.",
}


class AddReply(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class ReplyView(BaseModel):
    reply_id: uuid.UUID
    email_id: uuid.UUID
    category: ReplyCategory
    reason: str
    quote: str | None
    needs_human: bool
    decided_by_code: bool
    suppressed: bool
    next_step: str
    text: str
    created_at: dt.datetime


def _view(r: Reply) -> ReplyView:
    category = ReplyCategory(r.category)
    return ReplyView(
        reply_id=r.id, email_id=r.email_id, category=category, reason=r.reason, quote=r.quote,
        needs_human=r.needs_human, decided_by_code=r.decided_by_code, suppressed=r.suppressed,
        next_step=NEXT_STEP[category], text=r.body, created_at=r.created_at,
    )  # fmt: skip


@router.post("/emails/{email_id}/replies", status_code=201)
async def add_reply(email_id: uuid.UUID, body: AddReply, request: Request, team: Team) -> ReplyView:
    d = get_deps(request)
    if d.classify_reply is None:
        raise HTTPException(503, "reply classification is not configured on this server")
    async with d.sessions() as s:
        email = await _owned(s, email_id, team)
        if email.status is not EmailStatus.SENT:
            raise HTTPException(
                409, f"only a sent email can have replies (status: {email.status.value})"
            )
        if d.replies_per_key_per_day:
            midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            today = await s.scalar(
                select(func.count(Reply.id))
                .join(EmailDraft, Reply.email_id == EmailDraft.id)
                .join(Run, EmailDraft.run_id == Run.id)
                .where(Run.api_key_id == team.id, Reply.created_at >= midnight)
            )
            if (today or 0) >= d.replies_per_key_per_day:
                raise HTTPException(429, f"daily reply cap reached ({d.replies_per_key_per_day})")
        subject, sent_body, sent_to = email.subject, email.body, email.sent_to

    run = await d.classify_reply(subject, sent_body, body.text)
    result = run.result
    # Opt-out first: if storing the reply fails afterwards, the address is still protected.
    suppressed = result.category is ReplyCategory.UNSUBSCRIBE and bool(sent_to)
    if suppressed:
        await suppress(d.sessions, team.id, sent_to)
    async with d.sessions() as s:
        reply = Reply(
            email_id=email_id, body=body.text, category=result.category.value,
            reason=result.reason, quote=result.quote, needs_human=result.needs_human,
            decided_by_code=result.decided_by_code, suppressed=suppressed,
            cost_usd=estimate_cost_usd(run.agent.model, run.agent.usage) if run.agent else None,
            created_by=team.name,
        )  # fmt: skip
        s.add(reply)
        await s.commit()
        return _view(reply)


@router.get("/emails/{email_id}/replies")
async def list_email_replies(email_id: uuid.UUID, request: Request, team: Team) -> list[ReplyView]:
    async with get_deps(request).sessions() as s:
        await _owned(s, email_id, team)
        rows = await s.scalars(
            select(Reply).where(Reply.email_id == email_id).order_by(Reply.created_at.desc())
        )
        return [_view(r) for r in rows]


@router.get("/replies")
async def list_replies(
    request: Request, team: Team, category: ReplyCategory | None = None, limit: int = 50
) -> list[ReplyView]:
    q = (
        select(Reply)
        .join(EmailDraft, Reply.email_id == EmailDraft.id)
        .join(Run, EmailDraft.run_id == Run.id)
        .where(Run.api_key_id == team.id)
        .order_by(Reply.created_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if category is not None:
        q = q.where(Reply.category == category.value)
    async with get_deps(request).sessions() as s:
        return [_view(r) for r in await s.scalars(q)]
