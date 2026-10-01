"""Calibrate the faithfulness judge against human labels.

A judge score means nothing until we know how often the judge agrees with a person.

1. Export facts from an eval run into a CSV for labelling:
       uv run python -m evals.calibration export evals/results/<run>.json
2. Open evals/calibration/labels.csv and fill `human_label` with yes/no for every row:
   "yes" = every detail of the claim (each number, date, name, qualifier) is stated in its
   quotes; "no" = anything in the claim is missing from the quotes. Ignore whether the
   fact is true in the world: judge the claim against the quotes only.
3. Score the judge against your labels (spends a few cents per model):
       uv run python -m evals.calibration score
       uv run python -m evals.calibration score --judge-model claude-sonnet-5-5

The key number is the false-pass rate: of the claims YOU rejected, how many the judge
approved. Those are the errors that would let unsupported claims reach an email.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import anthropic

from app.config import get_settings
from app.schemas import Fact, ResearchReport
from evals.judge import FactVerdict, judge_report

CALIBRATION_DIR = Path(__file__).parent / "calibration"
LABELS_PATH = CALIBRATION_DIR / "labels.csv"
COLUMNS = [
    "domain", "fact_id", "source_url", "claim",
    "quote_1", "quote_2", "quote_3", "human_label", "notes",
]  # fmt: skip
YES = {"yes", "y", "1", "true"}
NO = {"no", "n", "0", "false"}


# --- export ---


def export_rows(results: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for r in results["results"]:
        for f in r["details"].get("facts", []):
            quotes = (f["evidence"] + ["", "", ""])[:3]
            rows.append(
                {
                    "domain": r["domain"],
                    "fact_id": f["id"],
                    "source_url": f["url"],
                    "claim": f["claim"],
                    "quote_1": quotes[0],
                    "quote_2": quotes[1],
                    "quote_3": quotes[2],
                    "human_label": "",
                    "notes": "",
                }
            )
    return rows


def write_labels(rows: list[dict[str, str]], path: Path, force: bool = False) -> None:
    if path.exists() and not force:
        raise SystemExit(f"{path} exists and may hold human labels; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


# --- score ---


def parse_label(raw: str) -> bool | None:
    v = raw.strip().lower()
    if v in YES:
        return True
    if v in NO:
        return False
    return None


def read_labels(path: Path) -> tuple[list[dict[str, str]], int]:
    """Return (labelled rows, number of unlabelled rows skipped)."""
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    labelled = [r for r in rows if parse_label(r["human_label"]) is not None]
    return labelled, len(rows) - len(labelled)


def rows_to_reports(rows: list[dict[str, str]]) -> dict[str, ResearchReport]:
    by_domain: dict[str, list[Fact]] = defaultdict(list)
    for r in rows:
        quotes = [r[k] for k in ("quote_1", "quote_2", "quote_3") if r[k].strip()]
        by_domain[r["domain"]].append(
            Fact(
                id=r["fact_id"],
                claim=r["claim"],
                category="other",  # the judge doesn't use the category
                source_url=r["source_url"],
                evidence=quotes,
            )
        )
    return {
        d: ResearchReport(domain=d, company_name=d, summary="", facts=facts)
        for d, facts in by_domain.items()
    }


def agreement(pairs: list[tuple[bool, bool]]) -> dict[str, Any]:
    """pairs = (human_faithful, judge_faithful)."""
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    agree = sum(h == j for h, j in pairs)
    human_no = [j for h, j in pairs if not h]
    human_yes = [j for h, j in pairs if h]
    false_pass = sum(human_no)  # human said no, judge said yes
    false_fail = sum(not j for j in human_yes)  # human said yes, judge said no

    # Cohen's kappa: agreement corrected for what two raters would hit by chance.
    po = agree / n
    p_h = sum(h for h, _ in pairs) / n
    p_j = sum(j for _, j in pairs) / n
    pe = p_h * p_j + (1 - p_h) * (1 - p_j)
    kappa = round((po - pe) / (1 - pe), 3) if pe < 1 else None

    return {
        "n": n,
        "human_faithful_rate": round(p_h, 3),
        "judge_faithful_rate": round(p_j, 3),
        "accuracy": round(po, 3),
        "false_pass": false_pass,
        "false_pass_rate": round(false_pass / len(human_no), 3) if human_no else None,
        "false_fail": false_fail,
        "false_fail_rate": round(false_fail / len(human_yes), 3) if human_yes else None,
        "kappa": kappa,
    }


async def score(path: Path, judge_model: str | None) -> None:
    s = get_settings()
    model = judge_model or s.model_fast
    rows, skipped = read_labels(path)
    if not rows:
        raise SystemExit(f"No labelled rows in {path}. Fill human_label with yes/no first.")

    client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key.get_secret_value())
    reports = rows_to_reports(rows)
    judgements = await asyncio.gather(
        *(judge_report(client=client, model=model, report=rep, golden_claims=[])
          for rep in reports.values())
    )  # fmt: skip
    verdicts: dict[tuple[str, str], FactVerdict] = {
        (d, v.fact_id): v for d, j in zip(reports, judgements, strict=True) for v in j.facts
    }

    pairs: list[tuple[bool, bool]] = []
    disagreements = []
    for r in rows:
        human = parse_label(r["human_label"])
        assert human is not None
        v = verdicts[(r["domain"], r["fact_id"])]
        pairs.append((human, v.faithful))
        if human != v.faithful:
            disagreements.append(
                {
                    "domain": r["domain"],
                    "fact_id": r["fact_id"],
                    "claim": r["claim"],
                    "human": "yes" if human else "no",
                    "judge": "yes" if v.faithful else "no",
                    "judge_unsupported": v.unsupported,
                    "human_notes": r["notes"],
                }
            )

    stats = agreement(pairs)
    print(f"judge model: {model}   labelled: {len(rows)}   skipped (unlabelled): {skipped}")
    print(json.dumps(stats, indent=2))
    for d in disagreements:
        print(f"\nDISAGREE {d['domain']} {d['fact_id']}: human={d['human']} judge={d['judge']}")
        print(f"  claim: {d['claim']}")
        if d["judge_unsupported"]:
            print(f"  judge says unsupported: {d['judge_unsupported']}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = CALIBRATION_DIR / f"score-{stamp}-{model}.json"
    out.write_text(
        json.dumps(
            {
                "judge_model": model,
                "labels": str(path),
                "stats": stats,
                "disagreements": disagreements,
            },
            indent=2,
            ensure_ascii=False,
        )  # fmt: skip
    )
    print(f"\nwrote {out}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="eval results JSON -> labels CSV")
    e.add_argument("results", type=Path)
    e.add_argument("--out", type=Path, default=LABELS_PATH)
    e.add_argument("--force", action="store_true", help="overwrite an existing labels file")
    sc = sub.add_parser("score", help="score the judge against human labels")
    sc.add_argument("--labels", type=Path, default=LABELS_PATH)
    sc.add_argument("--judge-model")
    args = p.parse_args()

    if args.cmd == "export":
        rows = export_rows(json.loads(args.results.read_text()))
        write_labels(rows, args.out, force=args.force)
        print(f"wrote {len(rows)} rows to {args.out}; fill human_label with yes/no")
    else:
        asyncio.run(score(args.labels, args.judge_model))


if __name__ == "__main__":
    sys.exit(main())
