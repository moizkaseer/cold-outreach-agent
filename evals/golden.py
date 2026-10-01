"""Golden set: real companies with hand-verified facts, one JSON file per company.

    evals/golden_set/<domain>.json

Facts are drafted (by Claude or a human) with `verified: false`. A human then opens the
source URL, confirms the claim, and sets `verified: true` + `verified_on`. Evals only
score against verified facts, so the set can grow without ever counting unchecked data.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from pathlib import Path

from pydantic import Field, HttpUrl, model_validator

from app.schemas.base import StrictModel
from app.schemas.lead import Lead
from app.schemas.research import FactCategory

GOLDEN_DIR = Path(__file__).parent / "golden_set"


class Segment(StrEnum):
    SAAS = "saas"
    DEV_TOOLS = "dev-tools"
    FINTECH = "fintech"
    ECOMMERCE = "ecommerce"
    AI = "ai"
    SERVICES = "services"
    OTHER = "other"


class Size(StrEnum):
    STARTUP = "startup"  # < ~100 people
    SCALEUP = "scaleup"  # ~100-1000
    ENTERPRISE = "enterprise"  # 1000+


class GoldenFact(StrictModel):
    claim: str = Field(min_length=1)
    category: FactCategory
    source_url: HttpUrl
    evidence: str = Field(min_length=1, description="Verbatim quote from source_url.")
    verified: bool = False
    verified_on: dt.date | None = None

    @model_validator(mode="after")
    def verified_needs_date(self) -> GoldenFact:
        if self.verified and self.verified_on is None:
            raise ValueError("verified facts need verified_on (when you checked the page)")
        return self


class GoldenCompany(StrictModel):
    domain: str
    company_name: str = Field(min_length=1)
    segment: Segment
    size: Size
    facts: list[GoldenFact] = Field(min_length=1)

    @model_validator(mode="after")
    def domain_is_normalised(self) -> GoldenCompany:
        # Same normalisation the pipeline uses, so file names match cache keys.
        canonical = Lead(domain=self.domain, person_name="x", person_role="x").domain
        if canonical != self.domain:
            raise ValueError(f"domain must be normalised: {self.domain!r} -> {canonical!r}")
        return self

    @property
    def verified_facts(self) -> list[GoldenFact]:
        return [f for f in self.facts if f.verified]


def load_golden_set(directory: Path = GOLDEN_DIR) -> list[GoldenCompany]:
    """Load and validate every company file. Raises on any invalid or misnamed file."""
    companies: list[GoldenCompany] = []
    for path in sorted(directory.glob("*.json")):
        company = GoldenCompany.model_validate_json(path.read_text(encoding="utf-8"))
        if path.stem != company.domain:
            raise ValueError(f"{path.name}: file name must be '{company.domain}.json'")
        companies.append(company)
    return companies
