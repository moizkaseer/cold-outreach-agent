from app.agents.loop import FINISH_TOOL_NAME
from app.agents.scorer import LeadScorer
from tests.fakes import FakeClient, response, tool_use
from tests.samples import LEAD, SENDER


async def test_score_out_of_range_is_retried() -> None:
    client = FakeClient(
        response(tool_use(FINISH_TOOL_NAME, {"score": 9, "reason": "r"})),
        response(tool_use(FINISH_TOOL_NAME, {"score": 4, "reason": "owns onboarding"}, id="t2")),
    )
    run = await LeadScorer(client=client, model="haiku").score(LEAD, SENDER)  # type: ignore[arg-type]
    assert run.score is not None and run.score.score == 4
    user = client.requests[0]["messages"][0]["content"]
    assert "Head of People" in user and "onboarding programs" in user
    assert "fallbacks" not in client.requests[0]  # Haiku
