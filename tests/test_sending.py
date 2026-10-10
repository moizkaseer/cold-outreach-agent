"""Sending, unsubscribe, suppression. A fake provider: no email is ever sent by tests."""

import ast
import json
import uuid
from pathlib import Path

import httpx
import pytest

from app.db.models import Base, EmailStatus
from app.db.session import make_engine, make_sessionmaker
from app.sending import OutboundEmail, ResendSender, SendError
from tests.samples import SENDER
from tests.test_api import NO_LIMITS, Env
from tests.test_review import BODY_TEXT, drafted, team

SEND = {"to": "Ana.Silva@Acme.com", "postal_address": "12 Example Street, Lahore, Pakistan"}


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[OutboundEmail, str]] = []
        self.fail = False

    async def send(self, msg: OutboundEmail, *, idempotency_key: str) -> str:
        if self.fail:
            raise SendError("provider answered 500")
        self.sent.append((msg, idempotency_key))
        return f"msg_{len(self.sent)}"


@pytest.fixture
async def env(tmp_path: Path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    e = Env(make_sessionmaker(engine), NO_LIMITS, mailer=FakeMailer(),
            public_base_url="https://api.example.com/", sends_per_key_per_day=2)  # fmt: skip
    yield e
    await engine.dispose()


async def approved(env: Env, client: httpx.AsyncClient) -> str:
    return await drafted(env, client, EmailStatus.APPROVED)


@pytest.mark.parametrize("status", [EmailStatus.VERIFIED, EmailStatus.NEEDS_HUMAN,
                                    EmailStatus.REJECTED, EmailStatus.SENT])  # fmt: skip
async def test_only_approved_emails_can_be_sent(env, status) -> None:
    acme = await team(env, "Acme")
    email_id = await drafted(env, acme, status)
    r = await acme.post(f"/emails/{email_id}/send", json=SEND)
    assert r.status_code == 409 and env.deps.mailer.sent == []


async def test_send_requires_the_owning_team(env) -> None:
    acme, rival = await team(env, "Acme"), await team(env, "Rival")
    email_id = await approved(env, acme)
    assert (await env.client.post(f"/emails/{email_id}/send", json=SEND)).status_code == 401
    assert (await rival.post(f"/emails/{email_id}/send", json=SEND)).status_code == 404
    assert env.deps.mailer.sent == []


async def test_send_adds_footer_headers_and_records_it(env) -> None:
    acme = await team(env, "Acme")
    email_id = await approved(env, acme)
    d = (await acme.post(f"/emails/{email_id}/send", json=SEND)).json()
    assert d["status"] == "sent" and d["sent_to"] == "ana.silva@acme.com" and d["sent_at"]

    (msg, key), = env.deps.mailer.sent  # fmt: skip
    assert key == email_id  # idempotency key: a retry can't become a second delivery
    assert msg.to == "ana.silva@acme.com" and msg.from_name == SENDER.name
    assert msg.text.startswith(BODY_TEXT)
    link = msg.headers["List-Unsubscribe"].strip("<>")
    assert link.startswith("https://api.example.com/unsubscribe/") and "//unsub" not in link[8:]
    assert msg.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    for required in (link, "12 Example Street, Lahore, Pakistan", SENDER.company):
        assert required in msg.text

    again = await acme.post(f"/emails/{email_id}/send", json=SEND)
    assert again.status_code == 409 and len(env.deps.mailer.sent) == 1  # never twice


@pytest.mark.parametrize("patch", [{"to": "not-an-email"}, {"postal_address": "x"},
                                   {"reply_to": "nope"}, {"to": None}])  # fmt: skip
async def test_bad_send_requests_are_422(env, patch) -> None:
    acme = await team(env, "Acme")
    email_id = await approved(env, acme)
    r = await acme.post(f"/emails/{email_id}/send", json={**SEND, **patch})
    assert r.status_code == 422 and env.deps.mailer.sent == []


async def test_provider_failure_leaves_the_email_approved_and_retryable(env) -> None:
    acme = await team(env, "Acme")
    email_id = await approved(env, acme)
    env.deps.mailer.fail = True
    r = await acme.post(f"/emails/{email_id}/send", json=SEND)
    assert r.status_code == 502
    d = (await acme.get(f"/emails/{email_id}")).json()
    assert d["status"] == "approved" and d["sent_to"] is None and d["sent_at"] is None

    env.deps.mailer.fail = False
    assert (await acme.post(f"/emails/{email_id}/send", json=SEND)).json()["status"] == "sent"


async def test_unsubscribe_link_works_and_blocks_future_sends(env) -> None:
    acme, rival = await team(env, "Acme"), await team(env, "Rival")
    await acme.post(f"/emails/{await approved(env, acme)}/send", json=SEND)
    link = env.deps.mailer.sent[0][0].headers["List-Unsubscribe"].strip("<>")
    path = link.removeprefix("https://api.example.com")

    # Opening the link (what a mail scanner does) must not unsubscribe by itself.
    page = await env.client.get(path)
    assert page.status_code == 200 and "<form" in page.text
    assert (await acme.get("/suppressions")).json() == []

    for _ in range(2):  # clicking twice is fine
        done = await env.client.post(path)
        assert done.status_code == 200 and "unsubscribed" in done.text
    assert [s["email"] for s in (await acme.get("/suppressions")).json()] == ["ana.silva@acme.com"]

    r = await acme.post(f"/emails/{await approved(env, acme)}/send", json=SEND)
    assert r.status_code == 409 and "unsubscribed" in r.json()["detail"]
    assert len(env.deps.mailer.sent) == 1
    # Opting out of one sender doesn't opt out of another (it is per team).
    assert (await rival.get("/suppressions")).json() == []


async def test_unknown_unsubscribe_token_looks_the_same(env) -> None:
    acme = await team(env, "Acme")
    r = await env.client.post(f"/unsubscribe/{uuid.uuid4().hex}")
    assert r.status_code == 200 and "unsubscribed" in r.text
    assert (await acme.get("/suppressions")).json() == []


async def test_manual_suppression(env) -> None:
    acme = await team(env, "Acme")
    for _ in range(2):
        r = await acme.post("/suppressions", json={"email": "Ana.Silva@acme.com"})
        assert r.status_code == 201
    assert len((await acme.get("/suppressions")).json()) == 1
    r = await acme.post(f"/emails/{await approved(env, acme)}/send", json=SEND)
    assert r.status_code == 409
    assert (await env.client.get("/suppressions")).status_code == 401


async def test_daily_cap_per_team(env) -> None:
    acme, rival = await team(env, "Acme"), await team(env, "Rival")
    for i in range(2):
        r = await acme.post(f"/emails/{await approved(env, acme)}/send",
                            json={**SEND, "to": f"p{i}@acme.com"})  # fmt: skip
        assert r.status_code == 200
    r = await acme.post(f"/emails/{await approved(env, acme)}/send", json=SEND)
    assert r.status_code == 429 and len(env.deps.mailer.sent) == 2
    r = await rival.post(f"/emails/{await approved(env, rival)}/send", json=SEND)
    assert r.status_code == 200  # another team has its own allowance


async def test_sending_is_off_unless_configured(env) -> None:
    acme = await team(env, "Acme")
    email_id = await approved(env, acme)
    env.deps.mailer = None
    r = await acme.post(f"/emails/{email_id}/send", json=SEND)
    assert r.status_code == 503
    assert (await acme.get(f"/emails/{email_id}")).json()["status"] == "approved"


# --- the Resend adapter, against a mock HTTP transport (never the real service) ---


async def test_resend_request_shape() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "re_123"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mailer = ResendSender("re_key", "sam@mail.example.com", client)
        msg = OutboundEmail(from_name='Sam "Evil" <x@y.z>\nBcc: a@b.c', to="ana@acme.com",
                            subject="Hi", text="Body", reply_to="sam@example.com",
                            headers={"List-Unsubscribe": "<https://x/u/1>"})  # fmt: skip
        assert await mailer.send(msg, idempotency_key="email-1") == "re_123"

    (req,) = seen
    assert str(req.url) == "https://api.resend.com/emails"
    assert req.headers["Authorization"] == "Bearer re_key"
    assert req.headers["Idempotency-Key"] == "email-1"
    body = json.loads(req.content)
    assert body["from"] == "Sam Evil x@y.zBcc: a@b.c <sam@mail.example.com>"  # can't break out
    assert body["to"] == ["ana@acme.com"] and body["text"] == "Body" and "html" not in body
    assert body["reply_to"] == "sam@example.com"


@pytest.mark.parametrize("response", [httpx.Response(422, json={"message": "bad from"}),
                                      httpx.Response(200, json={"nope": 1})])  # fmt: skip
async def test_resend_failures_raise_send_error(response) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)) as client:
        with pytest.raises(SendError):
            await ResendSender("k", "a@b.co", client).send(
                OutboundEmail("Sam", "x@y.co", "s", "t"), idempotency_key="1"
            )


async def test_resend_network_error_is_a_send_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        with pytest.raises(SendError):
            await ResendSender("k", "a@b.co", client).send(
                OutboundEmail("Sam", "x@y.co", "s", "t"), idempotency_key="1"
            )


def test_no_agent_or_pipeline_code_can_reach_sending() -> None:
    """Least privilege, enforced: only the API layer may import app.sending."""
    root = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for folder in ("agents", "tools", "pipeline", "workers"):
        for path in (root / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                if any(n.startswith(("app.sending", "app.api")) for n in names):
                    offenders.append(str(path.relative_to(root)))
    assert offenders == []
