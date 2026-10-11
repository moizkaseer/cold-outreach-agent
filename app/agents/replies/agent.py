"""Reply classifier (Haiku, no tools): sorts a prospect's reply so a human knows what to do.

The model only suggests. Code decides the two things that must not depend on it:
an explicit opt-out phrase always means UNSUBSCRIBE, and a category whose quote is not
really in the reply is not trusted (flagged for a human).
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic

from app.agents.loop import AgentResult, run_agent_loop
from app.agents.replies.prompts import SYSTEM_PROMPT, USER_TEMPLATE
from app.agents.replies.rules import asks_to_opt_out, strip_quoted
from app.schemas import ReplyCategory, ReplyClassification
from app.tools.text import quote_in_text

MAX_REPLY_CHARS = 4000  # a reply is a few lines; anything longer is cut before the model


@dataclass
class ClassifiedReply:
    category: ReplyCategory
    reason: str
    quote: str | None  # words from the reply that decided it; None if there are none
    needs_human: bool  # the category could not be backed up; a person must read the reply
    decided_by_code: bool = False  # True when the opt-out rule set the category


@dataclass
class ReplyRun:
    result: ClassifiedReply
    agent: AgentResult[ReplyClassification] | None  # None when no model call was made or it failed


class ReplyClassifier:
    def __init__(self, *, client: anthropic.AsyncAnthropic, model: str) -> None:
        self._client = client
        self._model = model

    async def classify(self, *, subject: str, body: str, reply: str) -> ReplyRun:
        own = strip_quoted(reply)[:MAX_REPLY_CHARS]
        if not own:
            return ReplyRun(
                ClassifiedReply(ReplyCategory.OTHER, "The reply has no text of its own.", None,
                                needs_human=True, decided_by_code=True),
                None,
            )  # fmt: skip
        try:
            agent = await run_agent_loop(
                name="replies",
                client=self._client,
                model=self._model,
                system=SYSTEM_PROMPT,
                user_message=USER_TEMPLATE.format(subject=subject, body=body, reply=own),
                tools=[],
                output_model=ReplyClassification,
                max_turns=3,
                token_budget=20_000,
                refusal_fallback=False,  # Haiku
            )
        except anthropic.APIError:
            # The model being down must not lose the reply or an opt-out in it: the code
            # rules still run, and anything else is left for a person.
            return ReplyRun(decide(own, None), None)
        return ReplyRun(decide(own, agent.output), agent)


def decide(own_text: str, model: ReplyClassification | None) -> ClassifiedReply:
    """Combine the model's suggestion with the code-checked rules."""
    phrase = asks_to_opt_out(own_text)
    if phrase and (model is None or model.category is not ReplyCategory.UNSUBSCRIBE):
        said = f" (the model said {model.category.value})" if model else ""
        return ClassifiedReply(ReplyCategory.UNSUBSCRIBE,
                               f"The reply contains an opt-out phrase{said}.", phrase,
                               needs_human=False, decided_by_code=True)  # fmt: skip
    if model is None:
        return ClassifiedReply(ReplyCategory.OTHER, "The classifier did not return a result.",
                               None, needs_human=True)  # fmt: skip
    if not quote_in_text(model.quote, own_text):
        return ClassifiedReply(model.category, model.reason, None, needs_human=True)
    return ClassifiedReply(model.category, model.reason, model.quote,
                           needs_human=model.category is ReplyCategory.OTHER)  # fmt: skip
