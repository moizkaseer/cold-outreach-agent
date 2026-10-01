import pytest

from app.agents.loop import FINISH_TOOL_NAME
from app.agents.writer import Revision, WriterAgent, lint_email
from app.schemas import Email, PainPoint
from tests.fakes import FakeClient, response, tool_use
from tests.samples import LEAD, REPORT, SENDER

PAIN = PainPoint(
    rank=1,
    title="Onboarding 120 new hires after the Series C",
    rationale="f1 + f2: fresh funding and 120 open roles mean a big onboarding load.",
    supporting_fact_ids=["f1", "f2"],
)

GOOD_BODY = (
    "Hi Ana,\n\n"
    "Congrats on the $60M Series C. With 120 open roles, getting new hires productive "
    "quickly is often the hard part.\n\n"
    "At Rampup we cut time-to-productivity for new hires by 30% at Northwind.\n\n"
    "Open to a 15-minute call next week?\n\nSam"
)


def email(body: str = GOOD_BODY, subject: str = "Your Series C hiring", ids=("f1", "f2")) -> Email:
    return Email(subject=subject, body=body, used_fact_ids=list(ids))


# --- lint_email ---


def test_good_email_passes() -> None:
    assert lint_email(email(), REPORT, SENDER) == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("I hope this finds you well. " + GOOD_BODY, "banned opener"),
        ("I hope you’re well! " + GOOD_BODY, "banned opener"),  # curly apostrophe
        (GOOD_BODY.replace("quickly", "quickly with AI"), "AI or automation"),
        (GOOD_BODY.replace("quickly", "via automation"), "AI or automation"),
        (GOOD_BODY.replace("Congrats", "**Congrats**"), "markdown bold"),
        (GOOD_BODY.replace("At Rampup", "- At Rampup"), "bullet list"),
        (GOOD_BODY.replace("Series C", "[Series C](https://x.y)"), "markdown link"),
        (GOOD_BODY.replace("Hi Ana", "<b>Hi Ana</b>"), "HTML tag"),
        (GOOD_BODY.replace("Open to a 15-minute call next week?", "Chat?"), "ask exactly"),
    ],
)
def test_lint_catches_rule_breaks(body: str, expected: str) -> None:
    problems = lint_email(email(body), REPORT, SENDER)
    assert any(expected in p for p in problems), problems


def test_lint_unknown_fact_ids_and_subject() -> None:
    problems = lint_email(email(ids=("f1", "f9"), subject="Automated hiring"), REPORT, SENDER)
    assert any("f9" in p for p in problems)
    assert any("AI or automation" in p for p in problems)  # subject is checked too


def test_ai_word_boundaries() -> None:
    # Words merely containing "ai" are fine; only the standalone term is banned.
    body = GOOD_BODY.replace("quickly", "quickly, Kai said, after the main training")
    assert lint_email(email(body), REPORT, SENDER) == []


# --- WriterAgent ---


def agent(client: FakeClient) -> WriterAgent:
    return WriterAgent(client=client, model="test-model")  # type: ignore[arg-type]


async def test_write_returns_email_and_lint_problems() -> None:
    bad = {"subject": "Hi", "body": "Use our AI. " + GOOD_BODY, "used_fact_ids": ["f1"]}
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, bad)))
    run = await agent(client).write(LEAD, REPORT, PAIN, SENDER)
    assert run.email is not None and run.email.subject == "Hi"
    assert any("AI or automation" in p for p in run.problems)

    user = client.requests[0]["messages"][0]["content"]
    assert "Onboarding 120 new hires" in user and "Northwind" in user
    assert "Revision" not in user  # first draft has no revision block


async def test_revision_includes_previous_draft_and_problems() -> None:
    good = {"subject": "s", "body": GOOD_BODY, "used_fact_ids": ["f1"]}
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, good)))
    rev = Revision(
        attempt=1, previous=email(subject="old subject"), problems=["claim X unsupported"]
    )
    run = await agent(client).write(LEAD, REPORT, PAIN, SENDER, revision=rev)
    assert run.problems == []
    user = client.requests[0]["messages"][0]["content"]
    assert "Revision 1" in user and "old subject" in user and "- claim X unsupported" in user


async def test_over_word_limit_is_retried_by_the_loop() -> None:
    long = {"subject": "s", "body": "word " * 130, "used_fact_ids": ["f1"]}
    good = {"subject": "s", "body": GOOD_BODY, "used_fact_ids": ["f1"]}
    client = FakeClient(
        response(tool_use(FINISH_TOOL_NAME, long)),
        response(tool_use(FINISH_TOOL_NAME, good, id="t2")),
    )
    run = await agent(client).write(LEAD, REPORT, PAIN, SENDER)
    assert run.email is not None and run.email.word_count < 120
    assert len(client.requests) == 2
