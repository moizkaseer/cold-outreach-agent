"""Database layer tests on SQLite (no server needed). Postgres is NOT exercised here."""

import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.models import Base, EmailDraft, EmailStatus, Lead, Run, RunStatus, Step, StepStatus
from app.db.session import make_engine, make_sessionmaker

ROOT = Path(__file__).parent.parent


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logging"] = False  # keep pytest's logging intact
    return cfg


def test_migrations_upgrade_and_downgrade(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    cfg = alembic_cfg(f"sqlite+aiosqlite:///{db}")
    command.upgrade(cfg, "head")
    tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master "
                                                         "where type='table'")}  # fmt: skip
    assert {"leads", "runs", "steps", "research_cache", "emails"} <= tables
    command.downgrade(cfg, "base")
    tables = {r[0] for r in sqlite3.connect(db).execute("select name from sqlite_master "
                                                         "where type='table'")}  # fmt: skip
    assert tables <= {"alembic_version"}


@pytest.fixture
async def session(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with make_sessionmaker(engine)() as s:
        yield s
    await engine.dispose()


async def make_run(session) -> Run:
    lead = Lead(domain="acme.com", person_name="Ana", person_role="CTO")
    run = Run(lead=lead, sender={"name": "Sam", "outcomes": ["x"]})
    session.add(run)
    await session.commit()
    return run


async def test_run_with_steps_and_email_round_trip(session) -> None:
    run = await make_run(session)
    session.add_all(
        [
            Step(run=run, name="research", position=4, status=StepStatus.SUCCEEDED,
                 output={"facts": [{"id": "f1"}]}, cost_usd=0.05),
            Step(run=run, name="validate", position=1, status=StepStatus.SUCCEEDED),
        ]
    )  # fmt: skip
    session.add(EmailDraft(run=run, subject="s", body="b", used_fact_ids=["f1"],
                           status=EmailStatus.VERIFIED))  # fmt: skip
    await session.commit()

    loaded = (await session.execute(select(Run).where(Run.id == run.id))).scalar_one()
    await session.refresh(loaded, ["steps", "email"])
    assert [s.name for s in loaded.steps] == ["validate", "research"]  # ordered by position
    assert loaded.steps[1].output == {"facts": [{"id": "f1"}]}
    assert loaded.status is RunStatus.PENDING and loaded.email.status is EmailStatus.VERIFIED


async def test_one_row_per_step_name_per_run(session) -> None:
    run = await make_run(session)
    session.add_all([Step(run=run, name="research", position=4),
                     Step(run=run, name="research", position=4)])  # fmt: skip
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_enums_stored_as_plain_strings(session, tmp_path: Path) -> None:
    run = await make_run(session)
    run.status = RunStatus.NEEDS_HUMAN
    await session.commit()
    raw = sqlite3.connect(tmp_path / "t.db").execute("select status from runs").fetchone()[0]
    assert raw == "needs_human"  # readable in SQL, no native enum type to migrate


async def test_one_email_per_run(session) -> None:
    run = await make_run(session)
    for _ in range(2):
        session.add(EmailDraft(run_id=run.id, subject="s", body="b", used_fact_ids=[],
                               status=EmailStatus.VERIFIED))  # fmt: skip
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_sqlite_drops_timezones_so_code_must_normalise(session) -> None:
    """Documents a portability trap: Postgres returns aware datetimes, SQLite naive ones.
    Any code comparing DB timestamps with now() must go through `as_utc`."""
    from app.db.models import as_utc

    run = await make_run(session)
    await session.refresh(run, ["created_at"])
    assert run.created_at.tzinfo is None  # SQLite round-trip lost the tz
    assert as_utc(run.created_at) <= dt.datetime.now(dt.UTC)  # safe comparison
    aware = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert as_utc(aware) is aware
