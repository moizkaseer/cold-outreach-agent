from enum import StrEnum
from typing import Annotated

from pydantic import Field, HttpUrl, model_validator

from app.schemas.base import StrictModel


class FactCategory(StrEnum):
    PRODUCT = "product"
    CUSTOMERS = "customers"
    FUNDING = "funding"
    HIRING = "hiring"
    NEWS = "news"
    TECH = "tech"
    OTHER = "other"


class Fact(StrictModel):
    id: str = Field(pattern=r"^f\d+$", description="Stable reference, e.g. 'f1'.")
    claim: str = Field(min_length=1, description="One atomic fact about the company.")
    category: FactCategory
    source_url: HttpUrl = Field(
        description="Page the fact came from. Required: no source, no fact."
    )
    evidence: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1,
        max_length=3,
        description=(
            "1-3 short verbatim quotes, each copied exactly from the source page. "
            "Together they must support every part of the claim."
        ),
    )


class ResearchReport(StrictModel):
    domain: str
    company_name: str = Field(min_length=1)
    summary: str = Field(description="2-3 sentence overview, only from facts below.")
    facts: list[Fact]

    @model_validator(mode="after")
    def unique_fact_ids(self) -> "ResearchReport":
        ids = [f.id for f in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("fact ids must be unique")
        return self

    def fact_ids(self) -> set[str]:
        return {f.id for f in self.facts}
