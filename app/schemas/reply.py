from enum import StrEnum

from pydantic import Field

from app.schemas.base import StrictModel


class ReplyCategory(StrEnum):
    INTERESTED = "interested"  # wants a call, a demo, more detail
    QUESTION = "question"  # asks something before deciding
    NOT_NOW = "not_now"  # maybe later: bad timing, "try me next quarter"
    NOT_INTERESTED = "not_interested"  # a plain no
    WRONG_PERSON = "wrong_person"  # not their area, often names someone else
    UNSUBSCRIBE = "unsubscribe"  # asks not to be contacted again
    OUT_OF_OFFICE = "out_of_office"  # automatic reply
    OTHER = "other"  # none of the above, or unclear


class ReplyClassification(StrictModel):
    """What the model returns. Code checks `quote` against the reply before trusting it."""

    category: ReplyCategory
    quote: str = Field(
        min_length=1,
        max_length=300,
        description="The words from the reply that decide the category, copied exactly.",
    )
    reason: str = Field(max_length=300, description="One short sentence.")
