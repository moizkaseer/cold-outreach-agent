from pydantic import Field, field_validator

from app.schemas.base import StrictModel

MAX_EMAIL_WORDS = 120  # CLAUDE.md: "under 120 words"


class Email(StrictModel):
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, description="Plain text, no markdown or HTML.")
    used_fact_ids: list[str] = Field(
        min_length=1, description="Report facts the email relies on; the verifier checks these."
    )

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    @field_validator("body")
    @classmethod
    def under_word_limit(cls, v: str) -> str:
        # Hard rule enforced in code, not just requested in the prompt.
        if (n := len(v.split())) >= MAX_EMAIL_WORDS:
            raise ValueError(f"body is {n} words; must be under {MAX_EMAIL_WORDS}")
        return v
