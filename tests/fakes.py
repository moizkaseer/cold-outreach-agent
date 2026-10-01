"""Scripted stand-ins for the Anthropic client, shared by agent tests."""

from types import SimpleNamespace
from typing import Any


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
