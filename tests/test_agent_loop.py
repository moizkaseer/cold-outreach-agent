"""Agent loop tests with a scripted fake client — no network, no API key, no cost.

Each test scripts what "the model" returns turn by turn and checks what the loop does.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from app.agents.loop import (
    FINISH_TOOL_NAME,
    NUDGE_MESSAGE,
    StopReason,
    Tool,
    run_agent_loop,
)

# --- fakes ---


def tool_use(name: str, input: dict[str, Any], id: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def response(*content: Any, stop: str = "tool_use", tokens: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(content),
        stop_reason=stop,
        usage=SimpleNamespace(input_tokens=tokens, output_tokens=10),
    )


class FakeClient:
    """Returns scripted responses in order and records every request."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> SimpleNamespace:
        # Snapshot messages: the loop keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self._responses.pop(0)


# --- a toy agent: one tool, one output model ---


class AddInput(BaseModel):
    a: int
    b: int


class Answer(BaseModel):
    value: int


calls: list[AddInput] = []


async def add(args: AddInput) -> str:
    calls.append(args)
    return str(args.a + args.b)


async def boom(args: AddInput) -> str:
    raise RuntimeError("network down")


ADD = Tool("add", "Add two integers.", AddInput, add)


async def run(client: FakeClient, tools: list[Tool] | None = None, **kw: Any):
    params = {"max_turns": 10, "token_budget": 1_000_000} | kw
    return await run_agent_loop(
        client=client,  # type: ignore[arg-type]
        model="test-model",
        system="sys",
        user_message="What is 2+3?",
        tools=tools if tools is not None else [ADD],
        output_model=Answer,
        **params,
    )


@pytest.fixture(autouse=True)
def _reset_calls() -> None:
    calls.clear()


# --- happy path ---


async def test_tool_then_finish() -> None:
    client = FakeClient(
        response(tool_use("add", {"a": 2, "b": 3})),
        response(tool_use(FINISH_TOOL_NAME, {"value": 5}, id="t2")),
    )
    result = await run(client)

    assert result.ok and result.output == Answer(value=5)
    assert result.turns == 2
    assert result.usage.total == 2 * 110
    assert [r.name for r in result.tool_calls] == ["add", FINISH_TOOL_NAME]

    # Turn 2 must have received the tool result, matched by tool_use_id.
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "5",
        "is_error": False,
    }


async def test_request_shape() -> None:
    client = FakeClient(response(tool_use(FINISH_TOOL_NAME, {"value": 1})))
    await run(client)
    req = client.requests[0]
    assert [t["name"] for t in req["tools"]] == ["add", FINISH_TOOL_NAME]
    assert req["tools"][1]["input_schema"]["required"] == ["value"]
    assert "tool_choice" not in req  # forced tool use 400s on Sonnet 5.5
    assert req["fallbacks"] == "default"


# --- tool failures are reported to the model, not raised ---


async def test_bad_tool_input_is_error_and_handler_not_called() -> None:
    client = FakeClient(
        response(tool_use("add", {"a": "two", "b": 3})),
        response(tool_use(FINISH_TOOL_NAME, {"value": 5}, id="t2")),
    )
    result = await run(client)
    assert result.ok
    assert calls == []
    first = result.tool_calls[0]
    assert first.is_error and "validation" in first.output


async def test_tool_exception_is_error_and_loop_continues() -> None:
    client = FakeClient(
        response(tool_use("add", {"a": 1, "b": 1})),
        response(tool_use(FINISH_TOOL_NAME, {"value": 2}, id="t2")),
    )
    result = await run(client, tools=[Tool("add", "d", AddInput, boom)])
    assert result.ok
    assert result.tool_calls[0].is_error
    assert "RuntimeError: network down" in result.tool_calls[0].output


async def test_unknown_tool() -> None:
    client = FakeClient(
        response(tool_use("send_email", {})),
        response(tool_use(FINISH_TOOL_NAME, {"value": 0}, id="t2")),
    )
    result = await run(client)
    assert result.ok and "Unknown tool" in result.tool_calls[0].output


async def test_parallel_calls_one_message_and_concurrent() -> None:
    started: list[int] = []
    gate = asyncio.Event()

    async def slow_add(args: AddInput) -> str:
        started.append(args.a)
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 1)  # deadlocks if run sequentially
        return str(args.a + args.b)

    client = FakeClient(
        response(tool_use("add", {"a": 1, "b": 1}, "x"), tool_use("add", {"a": 2, "b": 2}, "y")),
        response(tool_use(FINISH_TOOL_NAME, {"value": 6}, id="t2")),
    )
    result = await run(client, tools=[Tool("add", "d", AddInput, slow_add)])
    assert result.ok
    results_msg = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results_msg] == ["x", "y"]
    # A sequential run would time out above and surface as is_error results.
    assert [r["content"] for r in results_msg] == ["2", "4"]
    assert not any(r["is_error"] for r in results_msg)


# --- finish validation: retry once, then fail ---


async def test_invalid_finish_then_valid() -> None:
    client = FakeClient(
        response(tool_use(FINISH_TOOL_NAME, {"value": "five"})),
        response(tool_use(FINISH_TOOL_NAME, {"value": 5}, id="t2")),
    )
    result = await run(client)
    assert result.ok and result.output == Answer(value=5)
    feedback = client.requests[1]["messages"][-1]["content"][0]
    assert feedback["is_error"] and "finish again" in feedback["content"]


async def test_invalid_finish_twice_fails() -> None:
    bad = {"value": "five"}
    client = FakeClient(
        response(tool_use(FINISH_TOOL_NAME, bad)),
        response(tool_use(FINISH_TOOL_NAME, bad, id="t2")),
    )
    result = await run(client)
    assert result.stop_reason is StopReason.INVALID_OUTPUT and result.output is None


# --- model ends without calling finish ---


async def test_nudge_then_finish() -> None:
    client = FakeClient(
        response(text("The answer is 5."), stop="end_turn"),
        response(tool_use(FINISH_TOOL_NAME, {"value": 5})),
    )
    result = await run(client)
    assert result.ok
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": NUDGE_MESSAGE}


async def test_too_many_nudges() -> None:
    client = FakeClient(*[response(text("done"), stop="end_turn") for _ in range(3)])
    result = await run(client)
    assert result.stop_reason is StopReason.NO_FINISH and result.turns == 3


# --- hard limits ---


async def test_max_turns() -> None:
    client = FakeClient(*[response(tool_use("add", {"a": 1, "b": 1})) for _ in range(3)])
    result = await run(client, max_turns=3)
    assert result.stop_reason is StopReason.MAX_TURNS
    assert len(client.requests) == 3


async def test_token_budget_stops_before_next_call() -> None:
    client = FakeClient(*[response(tool_use("add", {"a": 1, "b": 1}), tokens=500)] * 5)
    result = await run(client, token_budget=1_000)
    # 510 after turn 1 (< 1000, continue), 1020 after turn 2 (>= 1000, stop).
    assert result.stop_reason is StopReason.TOKEN_BUDGET
    assert len(client.requests) == 2 and result.turns == 2


@pytest.mark.parametrize(
    ("stop", "expected"),
    [("refusal", StopReason.REFUSED), ("max_tokens", StopReason.TRUNCATED)],
)
async def test_refused_or_truncated_turn_runs_no_tools(stop: str, expected: StopReason) -> None:
    client = FakeClient(response(tool_use("add", {"a": 1, "b": 1}), stop=stop))
    result = await run(client)
    assert result.stop_reason is expected
    assert calls == [] and result.tool_calls == []


async def test_finish_name_is_reserved() -> None:
    with pytest.raises(ValueError, match="reserved"):
        await run(FakeClient(), tools=[Tool(FINISH_TOOL_NAME, "d", AddInput, add)])
