from pathlib import Path

import pytest

from evals.calibration import (
    agreement,
    export_rows,
    parse_label,
    read_labels,
    rows_to_reports,
    write_labels,
)

RESULTS = {
    "results": [
        {
            "domain": "acme.com",
            "details": {
                "facts": [
                    {
                        "id": "f1",
                        "claim": "c1",
                        "url": "https://acme.com/about",
                        "evidence": ["q1", "q2"],
                    },
                    {"id": "f2", "claim": "c2", "url": "https://acme.com/blog", "evidence": ["q3"]},
                ]
            },
        },
        {"domain": "failed.com", "details": {}},  # a failed run has no facts
    ]
}


def test_export_then_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    rows = export_rows(RESULTS)
    assert [r["fact_id"] for r in rows] == ["f1", "f2"]
    assert (rows[0]["quote_2"], rows[0]["quote_3"]) == ("q2", "")
    write_labels(rows, path)

    # Label one row, leave one blank.
    text = path.read_text().replace("q1,q2,,,", "q1,q2,,yes,")
    path.write_text(text)
    labelled, skipped = read_labels(path)
    assert [r["fact_id"] for r in labelled] == ["f1"] and skipped == 1

    [rep] = rows_to_reports(labelled).values()
    assert rep.facts[0].evidence == ["q1", "q2"]  # blank quote columns dropped


def test_export_never_overwrites_labels(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text("my precious labels")
    with pytest.raises(SystemExit, match="--force"):
        write_labels(export_rows(RESULTS), path)
    assert path.read_text() == "my precious labels"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("yes", True), (" Y ", True), ("no", False), ("0", False), ("", None), ("maybe", None)],
)
def test_parse_label(raw: str, expected: bool | None) -> None:
    assert parse_label(raw) is expected


def test_agreement_stats() -> None:
    # (human, judge)
    pairs = [(True, True)] * 6 + [(False, False)] * 2 + [(False, True)] * 2 + [(True, False)]
    s = agreement(pairs)
    assert s["n"] == 11
    assert s["accuracy"] == round(8 / 11, 3)
    assert s["false_pass"] == 2 and s["false_pass_rate"] == 0.5  # 2 of 4 human-no
    assert s["false_fail"] == 1 and s["false_fail_rate"] == round(1 / 7, 3)
    assert -1 <= s["kappa"] <= 1


def test_agreement_edge_cases() -> None:
    assert agreement([]) == {"n": 0}
    s = agreement([(True, True), (True, True)])
    assert s["false_pass_rate"] is None and s["kappa"] is None  # no chance-correction possible
