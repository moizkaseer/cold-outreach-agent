from urllib.parse import urlsplit

from pydantic import Field, field_validator

from app.schemas.base import StrictModel


class Lead(StrictModel):
    domain: str = Field(description="Company domain, normalised, e.g. 'stripe.com'.")
    person_name: str = Field(min_length=1, max_length=200)
    person_role: str = Field(min_length=1, max_length=200)

    @field_validator("domain")
    @classmethod
    def normalise_domain(cls, v: str) -> str:
        # Accept what users paste ("https://www.Stripe.com/about") and store one
        # canonical form, so dedupe and the per-domain research cache key match.
        v = v.strip().lower()
        host = urlsplit(v if "://" in v else f"//{v}").hostname or ""
        host = host.removeprefix("www.")
        if "." not in host or " " in host:
            raise ValueError(f"not a valid domain: {v!r}")
        return host
