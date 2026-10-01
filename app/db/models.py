"""Database models. Postgres (Supabase) in production, SQLite locally and in tests.

Durability lives here: every pipeline step writes its status and output to `steps` under
the run's `run_id`, so a failed run can resume from the failed step instead of starting
over. Only portable types are used (JSON, Uuid, timezone-aware DateTime); JSON columns
become JSONB on Postgres.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def as_utc(value: dt.datetime) -> dt.datetime:
    """Postgres returns timezone-aware datetimes; SQLite returns naive ones (we always
    store UTC). Normalise before comparing with utcnow(), or Python raises TypeError."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType}


def _enum(e: type[StrEnum]) -> SAEnum:
    # Store enum VALUES as plain strings (no native PG enum type to migrate later).
    return SAEnum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x])


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"  # produced an email draft for review
    NEEDS_HUMAN = "needs_human"  # draft exists but still has problems
    STOPPED = "stopped"  # deliberately ended early (duplicate lead, low score, no facts)
    FAILED = "failed"  # a step failed; resumable


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EmailStatus(StrEnum):
    VERIFIED = "verified"  # passed checks; awaiting human review
    NEEDS_HUMAN = "needs_human"  # flagged problems; awaiting human review
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"  # only ever after APPROVED


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    person_name: Mapped[str] = mapped_column(String(200))
    person_role: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    runs: Mapped[list[Run]] = relationship(back_populates="lead")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)  # run_id
    lead_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leads.id"), index=True)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.PENDING)
    current_step: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    # Snapshot of the sender profile used, so a resumed run writes with the same inputs.
    sender: Mapped[dict[str, Any]] = mapped_column(JSONType)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    lead: Mapped[Lead] = relationship(back_populates="runs")
    steps: Mapped[list[Step]] = relationship(
        back_populates="run", order_by="Step.position", cascade="all, delete-orphan"
    )
    email: Mapped[EmailDraft | None] = relationship(back_populates="run", uselist=False)


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (Index("uq_steps_run_name", "run_id", "name", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(32))
    position: Mapped[int] = mapped_column(Integer)  # order in the pipeline
    status: Mapped[StepStatus] = mapped_column(_enum(StepStatus), default=StepStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[Run] = relationship(back_populates="steps")


class ResearchCacheEntry(Base):
    """Research reports cached per domain (CLAUDE.md: 7 days)."""

    __tablename__ = "research_cache"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    report: Mapped[dict[str, Any]] = mapped_column(JSONType)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class EmailDraft(Base):
    __tablename__ = "emails"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), unique=True)
    subject: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    used_fact_ids: Mapped[list[Any]] = mapped_column(JSONType)
    status: Mapped[EmailStatus] = mapped_column(_enum(EmailStatus))
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    problems: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[Run] = relationship(back_populates="email")
