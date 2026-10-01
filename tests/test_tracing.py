"""Tracing tests: spans are captured in memory with logfire's `capfire` fixture."""

import json
from pathlib import Path

import pytest

from app.agents.loop import FINISH_TOOL_NAME, run_agent_loop
from app.db.models import Base
from app.db.session import make_engine, make_sessionmaker
from app.pipeline.orchestrator import Orchestrator, StepContext, StepResult
from tests.fakes import FakeClient, response, tool_use
from tests.samples import LEAD, SENDER
from tests.test_agent_loop import ADD, Answer

SECRET_PROMPT = "SYSTEM-PROMPT-CANARY-7f3a"
SECRET_USER = "USER-MESSAGE-CANARY-91bc"


def spans_by_msg(capfire) -> dict[str, dict]:
    spans = capfire.exporter.exported_spans_as_dict()
    return {s["attributes"]["logfire.msg"]: s["attributes"] for s in spans}


async def test_agent_llm_and_tool_spans(capfire) -> None:
    client = FakeClient(
        response(tool_use("add", {"a": 2, "b": 3}), tokens=1000),
        response(tool_use(FINISH_TOOL_NAME, {"value": 5}, id="t2"), tokens=1000),
    )
    await run_agent_loop(name="calc", client=client, model="claude-sonnet-5-5",  # type: ignore[arg-type]
                         system=SECRET_PROMPT, user_message=SECRET_USER, tools=[ADD],
                         output_model=Answer, max_turns=5, token_budget=100_000)  # fmt: skip
    spans = spans_by_msg(capfire)

    agent = spans["agent calc"]
    assert agent["model"] == "claude-sonnet-5-5" and agent["stop_reason"] == "finished"
    assert agent["turns"] == 2 and agent["input_tokens"] == 2000
    # 2000 in * $2/M + 20 out * $10/M
    assert agent["cost_usd"] == pytest.approx(0.004 + 0.0002)

    llm_calls = [a for m, a in spans.items() if m == "llm call"]
    assert llm_calls and llm_calls[-1]["input_tokens"] == 1000
    assert spans["tool add"]["is_error"] is False

    # Metadata only: prompts and user content never land in traces.
    everything = json.dumps(capfire.exporter.exported_spans_as_dict(), default=str)
    assert SECRET_PROMPT not in everything and SECRET_USER not in everything


async def test_pipeline_run_and_step_spans(capfire, tmp_path: Path) -> None:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = make_sessionmaker(engine)

    async def ok(ctx: StepContext) -> StepResult:
        return StepResult({}, cost_usd=0.02)

    async def broken(ctx: StepContext) -> StepResult:
        raise RuntimeError("exa down")

    orch = Orchestrator(sessions, [("validate", ok), ("research", broken)])
    run_id = await orch.create_run(LEAD, SENDER)
    await orch.execute(run_id)
    spans = spans_by_msg(capfire)

    assert spans["pipeline run"]["status"] == "failed"
    assert spans["pipeline run"]["run_id"] == str(run_id)
    assert spans["step validate"]["status"] == "succeeded"
    assert spans["step validate"]["cost_usd"] == 0.02
    assert spans["step research"]["status"] == "failed"
    assert "exa down" in spans["step research"]["error"]
    await engine.dispose()


def test_setup_tracing_without_token_sends_nothing() -> None:
    from app.observability import setup_tracing

    setup_tracing("test", None)  # must not raise or require an account
