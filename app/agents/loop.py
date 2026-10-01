"""A minimal, budgeted agent loop on the Anthropic SDK.

The whole "agent" idea in ~150 lines:

    call model -> model asks for tools -> we run them -> send results back -> repeat

until the model calls `finish` with output that validates against a Pydantic model, or a
hard limit stops it. Code owns the loop and every limit; the model only decides which
tool to call next. Nothing here is research-specific — agents plug in their own tools,
system prompt and output model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

FINISH_TOOL_NAME = "finish"

# Per-call output cap. Non-streaming requests should stay around this size to avoid
# HTTP timeouts; a single research turn never needs more.
MAX_TOKENS_PER_CALL = 16_000

# CLAUDE.md: on output validation failure, retry once, then mark the step failed.
MAX_FINISH_RETRIES = 1

# How many times we remind the model to call `finish` if it stops without it.
MAX_NUDGES = 2

# Server-side refusal fallback: if the model declines (safety classifier, often a false
# positive on scraped web text), the API re-runs the request on a fallback model inside
# the same call instead of just stopping.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

NUDGE_MESSAGE = (
    f"You stopped without calling the `{FINISH_TOOL_NAME}` tool. Continue the task; "
    f"when you are done, call `{FINISH_TOOL_NAME}` with your final answer."
)


class StopReason(StrEnum):
    FINISHED = "finished"  # `finish` called with valid output
    MAX_TURNS = "max_turns"
    TOKEN_BUDGET = "token_budget"
    INVALID_OUTPUT = "invalid_output"  # `finish` input failed validation too many times
    NO_FINISH = "no_finish"  # model kept ending its turn without calling `finish`
    REFUSED = "refused"
    TRUNCATED = "truncated"  # hit max_tokens mid-response; tool inputs may be cut off


@dataclass(frozen=True)
class Tool:
    """A tool the model may call. Input is validated by `input_model` before `handler` runs."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], Awaitable[str]]

    def to_api(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total(self) -> int:
        # Every turn re-sends the whole history, so input tokens grow each turn. That's
        # real spend, so it counts towards the budget.
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def add(self, u: Any) -> None:
        self.input_tokens += u.input_tokens
        self.output_tokens += u.output_tokens
        self.cache_read_input_tokens += getattr(u, "cache_read_input_tokens", None) or 0
        self.cache_creation_input_tokens += getattr(u, "cache_creation_input_tokens", None) or 0


@dataclass(frozen=True)
class ToolCallRecord:
    turn: int
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool


@dataclass
class AgentResult[T: BaseModel]:
    output: T | None
    stop_reason: StopReason
    turns: int
    usage: Usage
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stop_reason is StopReason.FINISHED


def _finish_tool_schema(output_model: type[BaseModel]) -> dict[str, Any]:
    return {
        "name": FINISH_TOOL_NAME,
        "description": (
            "Submit your final answer and end the task. Call this exactly once, only when "
            "you are done. The input must match the schema exactly."
        ),
        "input_schema": output_model.model_json_schema(),
    }


def _error_text(e: ValidationError) -> str:
    # Compact, model-readable errors; no pydantic doc URLs.
    return "Input failed validation:\n" + e.json(include_url=False, include_context=False)


async def run_agent_loop[T: BaseModel](
    *,
    client: anthropic.AsyncAnthropic,
    model: str,
    system: str,
    user_message: str,
    tools: list[Tool],
    output_model: type[T],
    max_turns: int,
    token_budget: int,
) -> AgentResult[T]:
    tools_by_name = {t.name: t for t in tools}
    if FINISH_TOOL_NAME in tools_by_name:
        raise ValueError(f"'{FINISH_TOOL_NAME}' is reserved")
    api_tools = [t.to_api() for t in tools] + [_finish_tool_schema(output_model)]

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    usage = Usage()
    records: list[ToolCallRecord] = []
    finish_failures = 0
    nudges = 0

    def done(reason: StopReason, turns: int, output: T | None = None) -> AgentResult[T]:
        return AgentResult(output, reason, turns, usage, records, messages)

    async def run_tool(turn: int, block: Any) -> dict[str, Any]:
        """Run one tool call. Failures become is_error results the model can react to —
        a broken tool should never crash the run."""
        tool = tools_by_name.get(block.name)
        if tool is None:
            text, is_error = f"Unknown tool '{block.name}'.", True
        else:
            try:
                args = tool.input_model.model_validate(block.input)
                text, is_error = await tool.handler(args), False
            except ValidationError as e:
                text, is_error = _error_text(e), True
            except Exception as e:  # tool bug or network error: report it, keep going
                text, is_error = f"Tool error: {type(e).__name__}: {e}", True
        records.append(ToolCallRecord(turn, block.name, dict(block.input), text, is_error))
        return {
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": text,
            "is_error": is_error,
        }

    for turn in range(1, max_turns + 1):
        # Budget is checked before each call: we never start a call we can't afford.
        if usage.total >= token_budget:
            return done(StopReason.TOKEN_BUDGET, turn - 1)

        # No forced tool_choice: Sonnet 5.5 rejects `any`/`tool` with a 400. The default
        # ("auto") plus the prompt steers it; the nudge below handles the rest.
        response = await client.beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS_PER_CALL,
            system=system,
            tools=api_tools,
            messages=messages,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            # Auto prompt caching: caches the longest stable prefix (tools, system, history
            # so far). Each turn re-sends everything, so this makes repeat input ~10x cheaper.
            cache_control={"type": "ephemeral"},
        )
        usage.add(response.usage)
        # Append the full content (not just text): tool_use and thinking blocks must be
        # sent back unchanged on the next call.
        messages.append({"role": "assistant", "content": response.content})

        # Never execute tools from a refused or truncated turn — their inputs may be
        # incomplete.
        if response.stop_reason == "refusal":
            return done(StopReason.REFUSED, turn)
        if response.stop_reason == "max_tokens":
            return done(StopReason.TRUNCATED, turn)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            nudges += 1
            if nudges > MAX_NUDGES:
                return done(StopReason.NO_FINISH, turn)
            messages.append({"role": "user", "content": NUDGE_MESSAGE})
            continue

        results: list[dict[str, Any]] = []
        finish_block = next((b for b in tool_uses if b.name == FINISH_TOOL_NAME), None)
        if finish_block is not None:
            try:
                output = output_model.model_validate(finish_block.input)
            except ValidationError as e:
                finish_failures += 1
                records.append(
                    ToolCallRecord(turn, FINISH_TOOL_NAME, dict(finish_block.input), str(e), True)
                )
                if finish_failures > MAX_FINISH_RETRIES:
                    return done(StopReason.INVALID_OUTPUT, turn)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": finish_block.id,
                        "content": _error_text(e) + "\nFix the errors and call finish again.",
                        "is_error": True,
                    }
                )
            else:
                records.append(
                    ToolCallRecord(turn, FINISH_TOOL_NAME, dict(finish_block.input), "", False)
                )
                return done(StopReason.FINISHED, turn, output)

        # Parallel tool calls run concurrently; all results go back in ONE user message
        # (splitting them teaches the model to stop calling tools in parallel).
        others = [b for b in tool_uses if b.name != FINISH_TOOL_NAME]
        results += await asyncio.gather(*(run_tool(turn, b) for b in others))
        messages.append({"role": "user", "content": results})

    return done(StopReason.MAX_TURNS, max_turns)
