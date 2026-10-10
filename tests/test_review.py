"""Human review API: queue, evidence, edit, approve, reject. Temp SQLite, fake queue."""

import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.api.auth import create_api_key
from app.api.review import EDITED_NOTE
from app.db.models import Base, EmailDraft, EmailStatus, Step, StepStatus
from app.db.session import make_engine, make_sessionmaker
from tests.samples import REPORT, SENDER
from tests.test_api import BODY, Env

BODY_TEXT = (
    "Saw Acme raised a $60M Series C. Teams hiring that fast often find ramp-up slips. "
    f"{SENDER.ask}"
)
PAIN = {"pain_points": [{"rank": 1, "title": "Slow ramp-up", "rationale": "Hiring fast.",
                         "supporting_fact_ids": ["f1"]}]}  # fmt: skip
CLAIMS = {"claims": [{"claim": "Acme raised a $60M Series C", "supported": True,
                      "fact_id": "f1", "reason": "quote 1"}], "feedback": ""}  # fmt: skip


@pytest.fixture
async def env(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    e = Env(make_sessionmaker(engine))
    yield e
    await engine.dispose()


async def team(env: Env, name: str) -> httpx.AsyncClient:
    _, raw = await create_api_key(env.sessions, name)
    return env.client_from("127.0.0.1", raw)


async def drafted(env: Env, client: httpx.AsyncClient,
                  status: EmailStatus = EmailStatus.VERIFIED) -> str:  # fmt: skip
    """A run that has reached review: research + pain points saved, email drafted."""
    run_id = uuid.UUID((await client.post("/runs", json=BODY)).json()["run_id"])
    outputs = {"research": {"report": REPORT.model_dump(mode="json")},
               "painpoints": {"pain_points": PAIN}}  # fmt: skip
    async with env.sessions() as s:
        for step in await s.scalars(select(Step).where(Step.run_id == run_id)):
            if step.name in outputs:
                step.output, step.status = outputs[step.name], StepStatus.SUCCEEDED
        email = EmailDraft(run_id=run_id, subject="Acme's Series C", body=BODY_TEXT,
                           used_fact_ids=["f1"], status=status, verification=CLAIMS,
                           problems=[])  # fmt: skip
        s.add(email)
        await s.commit()
        return str(email.id)


async def test_review_needs_a_key(env) -> None:
    acme = await team(env, "Acme")
    email_id = await drafted(env, acme)
    for call in (env.client.get("/emails"), env.client.get(f"/emails/{email_id}"),
                 env.client.post(f"/emails/{email_id}/approve")):  # fmt: skip
        assert (await call).status_code == 401


async def test_queue_shows_only_this_teams_undecided_emails(env) -> None:
    acme, rival = await team(env, "Acme"), await team(env, "Rival")
    waiting = await drafted(env, acme)
    flagged = await drafted(env, acme, EmailStatus.NEEDS_HUMAN)
    done = await drafted(env, acme, EmailStatus.APPROVED)
    await drafted(env, rival)

    queue = (await acme.get("/emails")).json()
    assert [e["email_id"] for e in queue] == [flagged, waiting]  # newest first
    assert queue[0]["lead"]["domain"] == "acme.com"
    approved = (await acme.get("/emails", params={"status": "approved"})).json()
    assert [e["email_id"] for e in approved] == [done]
    assert len((await acme.get("/emails", params={"status": "all"})).json()) == 3
    assert (await acme.get("/emails", params={"status": "nonsense"})).status_code == 422


async def test_detail_shows_the_evidence_behind_the_email(env) -> None:
    acme = await team(env, "Acme")
    email_id = await drafted(env, acme)
    d = (await acme.get(f"/emails/{email_id}")).json()
    assert d["status"] == "verified" and d["edited"] is False and d["word_count"] > 10
    # Only the facts the email uses (f1, not f2), each with its source URL and quotes.
    assert [s["fact_id"] for s in d["sources"]] == ["f1"]
    assert d["sources"][0]["source_url"] == "https://acme.com/blog/series-c"
    assert d["sources"][0]["evidence"][0] == "Today we announced our $60M Series C"
    assert d["claims"][0]["supported"] is True
    assert d["pain_point"]["title"] == "Slow ramp-up"

    view = (await acme.get(f"/runs/{d['run_id']}")).json()
    assert view["email"]["email_id"] == email_id  # the run links to its email


async def test_other_teams_cannot_see_or_decide(env) -> None:
    acme, rival = await team(env, "Acme"), await team(env, "Rival")
    email_id = await drafted(env, acme)
    assert (await rival.get(f"/emails/{email_id}")).status_code == 404
    assert (await rival.post(f"/emails/{email_id}/approve")).status_code == 404
    assert (await rival.post(f"/emails/{email_id}/reject", json={"reason": "x"})).status_code == 404
    assert (await rival.patch(f"/emails/{email_id}", json={"subject": "hi"})).status_code == 404
    assert (await acme.get(f"/emails/{email_id}")).json()["status"] == "verified"  # untouched


async def test_approve_records_who_and_when_and_is_final(env) -> None:
    acme = await team(env, "Acme sales")
    email_id = await drafted(env, acme, EmailStatus.NEEDS_HUMAN)  # humans may overrule flags
    d = (await acme.post(f"/emails/{email_id}/approve", json={"note": "fine"})).json()
    assert d["status"] == "approved" and d["reviewed_by"] == "Acme sales"
    assert d["reviewed_at"] and d["review_note"] == "fine"

    for again in (acme.post(f"/emails/{email_id}/approve"),
                  acme.post(f"/emails/{email_id}/reject", json={"reason": "changed my mind"}),
                  acme.patch(f"/emails/{email_id}", json={"body": "New text."})):  # fmt: skip
        r = await again
        assert r.status_code == 409 and "approved" in r.json()["detail"]
    after = (await acme.get(f"/emails/{email_id}")).json()
    assert after["body"] == BODY_TEXT  # approved text can't change underneath the approval


async def test_reject_needs_a_reason(env) -> None:
    acme = await team(env, "Acme")
    email_id = await drafted(env, acme)
    assert (await acme.post(f"/emails/{email_id}/reject", json={})).status_code == 422
    assert (await acme.post(f"/emails/{email_id}/reject", json={"reason": ""})).status_code == 422
    d = (await acme.post(f"/emails/{email_id}/reject", json={"reason": "opener is generic"})).json()
    assert d["status"] == "rejected" and d["review_note"] == "opener is generic"
    assert (await acme.get("/emails")).json() == []  # left the queue


async def test_edit_keeps_the_original_and_drops_the_verified_badge(env) -> None:
    acme = await team(env, "Acme")
    email_id = await drafted(env, acme)
    new_body = f"Congrats on the Series C. {SENDER.ask}"
    d = (await acme.patch(f"/emails/{email_id}", json={"body": new_body})).json()
    assert d["body"] == new_body and d["subject"] == "Acme's Series C"  # subject untouched
    assert d["edited"] is True and d["original_body"] == BODY_TEXT
    assert d["status"] == "needs_human" and d["problems"] == [EDITED_NOTE]

    # A second edit keeps the FIRST original (the machine's draft), not the first edit.
    d = (await acme.patch(f"/emails/{email_id}", json={"subject": "Quick question"})).json()
    assert d["subject"] == "Quick question" and d["body"] == new_body
    assert d["original_body"] == BODY_TEXT and d["original_subject"] == "Acme's Series C"

    assert (await acme.post(f"/emails/{email_id}/approve")).json()["status"] == "approved"


async def test_edit_rechecks_the_rules(env) -> None:
    acme = await team(env, "Acme")
    email_id = await drafted(env, acme)
    # Hard rule: under 120 words. Refused, nothing saved.
    r = await acme.patch(f"/emails/{email_id}", json={"body": "word " * 120})
    assert r.status_code == 422
    assert (await acme.get(f"/emails/{email_id}")).json()["edited"] is False
    assert (await acme.patch(f"/emails/{email_id}", json={})).status_code == 422

    # Style rules: saved, but the reviewer is told (no ask, mentions AI).
    d = (await acme.patch(f"/emails/{email_id}", json={"body": "Our AI can help."})).json()
    assert d["problems"][0] == EDITED_NOTE
    assert any("AI" in p for p in d["problems"]) and any("ask" in p for p in d["problems"])
