from pydantic import Field

from app.schemas.base import StrictModel


class ClaimCheck(StrictModel):
    claim: str = Field(min_length=1, description="A factual claim found in the email.")
    supported: bool
    fact_id: str | None = Field(default=None, description="Supporting fact, if any.")
    reason: str = Field(min_length=1)


class Verification(StrictModel):
    claims: list[ClaimCheck]
    feedback: str = Field(default="", description="What the writer must fix, if anything.")

    # Derived, not LLM-provided: the model can't say "passed" while listing an
    # unsupported claim. A plain property (not computed_field) so model_dump() output
    # reloads cleanly under extra="forbid" when a run resumes from the DB.
    @property
    def passed(self) -> bool:
        return all(c.supported for c in self.claims)

    @property
    def unsupported(self) -> list[ClaimCheck]:
        return [c for c in self.claims if not c.supported]
