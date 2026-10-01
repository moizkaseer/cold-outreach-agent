import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.golden import GoldenCompany, load_golden_set


def company(**kw) -> dict:
    fact = {
        "claim": "Acme serves over 4,000 customers.",
        "category": "customers",
        "source_url": "https://acme.com/about",
        "evidence": "now serves over 4,000 customers",
    }
    return {
        "domain": "acme.com",
        "company_name": "Acme",
        "segment": "saas",
        "size": "startup",
        "facts": [fact],
    } | kw


def write(tmp_path: Path, name: str, data: dict) -> None:
    (tmp_path / name).write_text(json.dumps(data))


def test_real_golden_set_is_valid() -> None:
    # Guards the committed files: every one must load cleanly.
    companies = load_golden_set()
    assert len({c.domain for c in companies}) == len(companies)


def test_load_and_verified_filter(tmp_path: Path) -> None:
    data = company()
    data["facts"].append(data["facts"][0] | {"verified": True, "verified_on": "2026-10-02"})
    write(tmp_path, "acme.com.json", data)
    [c] = load_golden_set(tmp_path)
    assert len(c.facts) == 2 and len(c.verified_facts) == 1


def test_file_name_must_match_domain(tmp_path: Path) -> None:
    write(tmp_path, "wrong.com.json", company())
    with pytest.raises(ValueError, match="file name"):
        load_golden_set(tmp_path)


def test_domain_must_be_normalised() -> None:
    with pytest.raises(ValidationError, match="normalised"):
        GoldenCompany.model_validate(company(domain="www.acme.com"))


def test_verified_needs_date() -> None:
    data = company()
    data["facts"][0]["verified"] = True
    with pytest.raises(ValidationError, match="verified_on"):
        GoldenCompany.model_validate(data)
