from pydantic import Field, model_validator

from app.schemas.base import StrictModel
from app.schemas.research import ResearchReport


class PainPoint(StrictModel):
    rank: int = Field(ge=1, description="1 = most relevant.")
    title: str = Field(min_length=1)
    rationale: str = Field(min_length=1, description="Why the evidence implies this pain.")
    supporting_fact_ids: list[str] = Field(min_length=1)


class PainPoints(StrictModel):
    pain_points: list[PainPoint] = Field(min_length=1)

    @model_validator(mode="after")
    def ranks_are_1_to_n(self) -> "PainPoints":
        ranks = sorted(p.rank for p in self.pain_points)
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError(f"ranks must be 1..N with no gaps/duplicates, got {ranks}")
        self.pain_points.sort(key=lambda p: p.rank)
        return self

    @property
    def top(self) -> PainPoint:
        return self.pain_points[0]

    def unknown_fact_ids(self, report: ResearchReport) -> set[str]:
        """Fact ids cited here that don't exist in the report (i.e. ungrounded)."""
        cited = {fid for p in self.pain_points for fid in p.supporting_fact_ids}
        return cited - report.fact_ids()
