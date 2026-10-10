"""Human review: no email leaves this system without a person approving it.

    GET   /emails                 this team's review queue (default: awaiting review)
    GET   /emails/{id}            the draft with its evidence: every fact and source URL
    PATCH /emails/{id}            edit subject/body before deciding
    POST  /emails/{id}/approve
    POST  /emails/{id}/reject     reason required

All routes need an API key and only ever touch that team's emails. The reviewer recorded
is the key's name. Decisions and edits are conditional UPDATEs on the current status, so
two reviewers can't both decide one email, and the text of an approved email can't change
underneath the approval.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.writer.rules import lint_email
from app.api.deps import Team, get_deps
from app.db.models import ApiKey, EmailDraft, EmailStatus, Run, Step, utcnow
from app.schemas import Email, ResearchReport, Sender

router = APIRouter(prefix="/emails", tags=["review"])

REVIEWABLE = (EmailStatus.VERIFIED, EmailStatus.NEEDS_HUMAN)
# The verifier passed the ORIGINAL text. After an edit nothing has checked the new text,
# so the email says so instead of keeping a "verified" badge it no longer earned.
EDITED_NOTE = "edited by a reviewer after verification; its claims were not re-checked"

QueueFilter = Literal["to_review", "approved", "rejected", "sent", "all"]
FILTERS: dict[str, tuple[EmailStatus, ...]] = {
    "to_review": REVIEWABLE,
    "approved": (EmailStatus.APPROVED,),
    "rejected": (EmailStatus.REJECTED,),
    "sent": (EmailStatus.SENT,),
    "all": tuple(EmailStatus),
}
PageLimit = Annotated[int, Query(ge=1, le=100)]
Before = Annotated[dt.datetime | None, Query(description="created_at of the last row seen")]


class LeadView(BaseModel):
    domain: str
    person_name: str
    person_role: str


class EmailSummary(BaseModel):
    email_id: uuid.UUID
    run_id: uuid.UUID
    lead: LeadView
    subject: str
    status: str
    problems: int
    created_at: dt.datetime


class SourceView(BaseModel):
    """One researched fact the email relies on, with where it came from."""

    fact_id: str
    claim: str
    source_url: str
    evidence: list[str]


class EmailDetail(BaseModel):
    email_id: uuid.UUID
    run_id: uuid.UUID
    lead: LeadView
    subject: str
    body: str
    word_count: int
    status: str
    problems: list[str]
    edited: bool
    original_subject: str | None
    original_body: str | None
    sources: list[SourceView]
    claims: list[dict[str, Any]]  # the verifier's claim-by-claim check of the ORIGINAL draft
    pain_point: dict[str, Any] | None
    reviewed_by: str | None
    reviewed_at: dt.datetime | None
    review_note: str | None
    created_at: dt.datetime


class EditEmail(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=120)
    body: str | None = Field(default=None, min_length=1, max_length=5000)

    @model_validator(mode="after")
    def something_to_change(self) -> EditEmail:
        if self.subject is None and self.body is None:
            raise ValueError("give a subject, a body, or both")
        return self


class Approve(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class Reject(BaseModel):
    # Required: "why not" is the most useful signal this product gets about its drafts.
    reason: str = Field(min_length=1, max_length=500)


def _lead(run: Run) -> LeadView:
    return LeadView(domain=run.lead.domain, person_name=run.lead.person_name,
                    person_role=run.lead.person_role)  # fmt: skip


async def _owned(s: AsyncSession, email_id: uuid.UUID, team: ApiKey) -> EmailDraft:
    """The email if it belongs to this team; otherwise 404 (same as "doesn't exist")."""
    email = (
        await s.execute(
            select(EmailDraft)
            .join(Run, EmailDraft.run_id == Run.id)
            .where(EmailDraft.id == email_id, Run.api_key_id == team.id)
            .options(selectinload(EmailDraft.run).selectinload(Run.lead))
        )
    ).scalar_one_or_none()
    if email is None:
        raise HTTPException(404, "email not found")
    return email


async def _step_outputs(s: AsyncSession, run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    rows = await s.execute(
        select(Step.name, Step.output).where(
            Step.run_id == run_id, Step.name.in_(["research", "painpoints"])
        )
    )
    return {name: output or {} for name, output in rows}


async def _detail(s: AsyncSession, email: EmailDraft) -> EmailDetail:
    outputs = await _step_outputs(s, email.run_id)
    facts = outputs.get("research", {}).get("report", {}).get("facts", [])
    used = set(email.used_fact_ids)
    pain_points = outputs.get("painpoints", {}).get("pain_points", {}).get("pain_points", [])
    return EmailDetail(
        email_id=email.id,
        run_id=email.run_id,
        lead=_lead(email.run),
        subject=email.subject,
        body=email.body,
        word_count=len(email.body.split()),
        status=email.status.value,
        problems=list(email.problems),
        edited=email.original_body is not None,
        original_subject=email.original_subject,
        original_body=email.original_body,
        sources=[
            SourceView(
                fact_id=f["id"],
                claim=f["claim"],
                source_url=str(f["source_url"]),
                evidence=list(f["evidence"]),
            )
            for f in facts
            if f["id"] in used
        ],  # fmt: skip
        claims=list((email.verification or {}).get("claims", [])),
        pain_point=pain_points[0] if pain_points else None,
        reviewed_by=email.reviewed_by,
        reviewed_at=email.reviewed_at,
        review_note=email.review_note,
        created_at=email.created_at,
    )


@router.get("")
async def review_queue(
    request: Request,
    team: Team,
    status: QueueFilter = "to_review",
    limit: PageLimit = 20,
    before: Before = None,
) -> list[EmailSummary]:
    q = (
        select(EmailDraft)
        .join(Run, EmailDraft.run_id == Run.id)
        .where(Run.api_key_id == team.id, EmailDraft.status.in_(FILTERS[status]))
        .options(selectinload(EmailDraft.run).selectinload(Run.lead))
        .order_by(EmailDraft.created_at.desc())
        .limit(limit)
    )
    if before is not None:
        q = q.where(EmailDraft.created_at < before)
    async with get_deps(request).sessions() as s:
        emails = (await s.execute(q)).scalars().all()
    return [
        EmailSummary(email_id=e.id, run_id=e.run_id, lead=_lead(e.run), subject=e.subject,
                     status=e.status.value, problems=len(e.problems), created_at=e.created_at)
        for e in emails
    ]  # fmt: skip


@router.get("/{email_id}")
async def get_email(email_id: uuid.UUID, request: Request, team: Team) -> EmailDetail:
    async with get_deps(request).sessions() as s:
        return await _detail(s, await _owned(s, email_id, team))


@router.patch("/{email_id}")
async def edit_email(
    email_id: uuid.UUID, body: EditEmail, request: Request, team: Team
) -> EmailDetail:
    async with get_deps(request).sessions() as s:
        email = await _owned(s, email_id, team)
        subject = body.subject if body.subject is not None else email.subject
        text = body.body if body.body is not None else email.body
        try:  # the hard rules (non-empty, under 120 words) bind people too
            draft = Email(subject=subject, body=text, used_fact_ids=list(email.used_fact_ids))
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e

        # Style rules are re-checked and shown, but don't block: the reviewer decides.
        problems = [EDITED_NOTE]
        report = (await _step_outputs(s, email.run_id)).get("research", {}).get("report")
        if report is not None:
            problems += lint_email(draft, ResearchReport.model_validate(report),
                                   Sender.model_validate(email.run.sender))  # fmt: skip

        changed = await s.execute(
            update(EmailDraft)
            .where(EmailDraft.id == email_id, EmailDraft.status.in_(REVIEWABLE))
            .values(
                subject=subject,
                body=text,
                # Keep the machine's draft from the FIRST edit on: comparing it with what
                # people actually send is real evidence about draft quality.
                original_subject=email.original_subject or email.subject,
                original_body=email.original_body or email.body,
                status=EmailStatus.NEEDS_HUMAN,
                problems=problems,
            )
        )
        await s.commit()
        if changed.rowcount != 1:
            raise HTTPException(409, f"email is already {email.status.value}; it can't be edited")
        s.expire_all()
        return await _detail(s, await _owned(s, email_id, team))


async def _decide(request: Request, email_id: uuid.UUID, team: ApiKey, status: EmailStatus,
                  note: str | None) -> EmailDetail:  # fmt: skip
    async with get_deps(request).sessions() as s:
        email = await _owned(s, email_id, team)
        decided = await s.execute(
            update(EmailDraft)
            .where(EmailDraft.id == email_id, EmailDraft.status.in_(REVIEWABLE))
            .values(status=status, reviewed_by=team.name, reviewed_at=utcnow(), review_note=note)
        )
        await s.commit()
        if decided.rowcount != 1:
            raise HTTPException(409, f"email is already {email.status.value}")
        s.expire_all()
        return await _detail(s, await _owned(s, email_id, team))


@router.post("/{email_id}/approve")
async def approve_email(
    email_id: uuid.UUID, request: Request, team: Team, body: Approve | None = None
) -> EmailDetail:
    """Approval only records the decision. Nothing is sent by approving."""
    note = body.note if body else None
    return await _decide(request, email_id, team, EmailStatus.APPROVED, note)


@router.post("/{email_id}/reject")
async def reject_email(
    email_id: uuid.UUID, body: Reject, request: Request, team: Team
) -> EmailDetail:
    return await _decide(request, email_id, team, EmailStatus.REJECTED, body.reason)
