"""Run the agent loop once against the real API with a toy tool.

    uv run python -m scripts.loop_smoke

Costs a fraction of a cent. Shows each tool call, the stop reason and token usage.
"""

import asyncio

import anthropic
from pydantic import BaseModel, Field

from app.agents.loop import Tool, run_agent_loop
from app.config import get_settings


class MultiplyInput(BaseModel):
    a: int
    b: int


class Answer(BaseModel):
    value: int = Field(description="The final numeric answer.")
    explanation: str = Field(description="One sentence on how you got it.")


async def multiply(args: MultiplyInput) -> str:
    return str(args.a * args.b)


async def main() -> None:
    s = get_settings()
    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
    result = await run_agent_loop(
        client=client,
        model=s.model_smart,
        system="You are a careful calculator. Use the multiply tool for every multiplication.",
        user_message="What is (17 * 23) * 4? Then submit the answer with the finish tool.",
        tools=[
            Tool(
                "multiply",
                "Multiply two integers. Use for all multiplication.",
                MultiplyInput,
                multiply,
            )
        ],
        output_model=Answer,
        max_turns=s.research_max_turns,
        token_budget=s.research_max_tokens,
    )
    for c in result.tool_calls:
        print(
            f"turn {c.turn}: {c.name}({c.input}) -> {c.output!r}{' [ERROR]' if c.is_error else ''}"
        )
    print(f"stop={result.stop_reason} turns={result.turns} tokens={result.usage}")
    print("output:", result.output)


if __name__ == "__main__":
    asyncio.run(main())
