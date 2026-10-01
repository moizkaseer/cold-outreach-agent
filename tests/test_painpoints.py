from typing import Any

import pytest

from app.agents.loop import FINISH_TOOL_NAME, StopReason
from app.agents.painpoints import PainPointAgent
from tests.fakes import FakeClient, response, tool_use
from tests.samples import LEAD, REPORT, SENDER


def pp(rank: int, ids: list[str], title: str = "t") -> dict[str, Any]:
    return {"rank": rank, "title": title, "rationale": "r", "supporting_fact_ids": ids}


def agent(client: FakeClient) -> PainPointAgent:
    return PainPointAgent(client=client, model="test-model")  # type: ignore[arg-type]


async def test_grounded_pain_points_kept_and_reranked() -> None:
    client = FakeClient(
        response(
            tool_use(
                FINISH_TOOL_NAME,
                {
                    "pain_points": [
                        pp(1, ["f9"], "invented"),  # cites a fact that doesn't exist
                        pp(2, ["f1", "f2"], "onboarding at speed"),
                        pp(3, ["f2"], "recruiting load"),
                    ]
                },
            )
        )
    )
    run = await agent(client).run(LEAD, REPORT, SENDER)

    assert run.pain_points is not None
    assert [(p.rank, p.title) for p in run.pain_points.pain_points] == [
        (1, "onboarding at speed"),
        (2, "recruiting load"),
    ]
    assert run.pain_points.top.title == "onboarding at speed"
    assert [p.title for p in run.dropped] == ["invented"]


async def test_all_ungrounded_means_no_pain_points() -> None:
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, {"pain_points": [pp(1, ["f7"])]})))
    run = await agent(client).run(LEAD, REPORT, SENDER)
    assert run.pain_points is None and len(run.dropped) == 1
    assert run.agent.stop_reason is StopReason.FINISHED  # model finished; code rejected


async def test_prompt_contents() -> None:
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, {"pain_points": [pp(1, ["f1"])]})))
    await agent(client).run(LEAD, REPORT, SENDER)
    req = client.requests[0]
    user = req["messages"][0]["content"]
    assert "Head of People" in user and "onboarding programs" in user
    assert '"id": "f1"' in user and "<research" in user
    assert [t["name"] for t in req["tools"]] == [FINISH_TOOL_NAME]  # no other tools
    assert "acme" not in req["system"].lower()  # static, cacheable system prompt


async def test_empty_report_is_rejected_before_any_call() -> None:
    client = FakeClient()
    with pytest.raises(ValueError, match="no facts"):
        await agent(client).run(LEAD, REPORT.model_copy(update={"facts": []}), SENDER)
    assert client.requests == []
