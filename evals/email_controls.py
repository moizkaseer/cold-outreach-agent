"""Planted-flaw controls for the email judge: labels that need no human expert.

The rubric judge passed 80/80 on our 10-lead fixture, so it can't separate good emails
from bad ones. Human labels from someone who isn't an SDR wouldn't fix that. Instead, take
real emails and plant ONE known flaw each. The correct verdict is then known by
construction: the judge must fail the targeted criterion on the flawed copy.

Three numbers come out of this:
- catch rate per flaw: how often the targeted criterion fails on the flawed copy
  (low = judge too lenient for that criterion);
- pass rate on the unedited originals (low = judge harsh, OR the original really is weak;
  originals are "no planted flaw", not "known good");
- flip rate across repeated runs on the same email (high = judge too noisy to trust).

The edits are deterministic text surgery, so the same fixture always gives the same cases.
The code-checked criteria (lint, number count) are deterministic and unit-tested already,
so only the LLM criteria are measured here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from statistics import mean
from typing import Any

from pydantic import ValidationError

from app.schemas import Email, Lead, PainPoint, ResearchReport, Sender
from evals.email_judge import CRITERIA

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_INT = re.compile(r"\b\d+\b")


@dataclass(frozen=True)
class Case:
    domain: str
    lead: Lead
    report: ResearchReport
    pain_point: PainPoint
    email: Email
    flaw: str | None = None  # None = the unedited original
    target: str | None = None  # criterion the planted flaw must fail


# --- text surgery helpers ---


def _paragraphs(body: str) -> list[str]:
    return body.split("\n\n")


def _sentences(paragraph: str) -> list[str]:
    return _SENTENCE_END.split(paragraph)


def _ask_index(paras: list[str], sender: Sender) -> int | None:
    return next((i for i, p in enumerate(paras) if sender.ask in p), None)


def _outcome_location(paras: list[str], sender: Sender) -> tuple[int, int] | None:
    """(paragraph, sentence) of the last sentence that cites the sender's outcome numbers.

    The LAST one, because company numbers can collide with outcome numbers earlier in the
    email (PostHog's "10+ paid products" vs the outcome's "10 weeks").
    """
    numbers = set(_INT.findall(" ".join(sender.outcomes)))
    found = None
    for pi, p in enumerate(paras):
        if sender.ask in p:
            continue
        for si, s in enumerate(_sentences(p)):
            if numbers & set(_INT.findall(s)):
                found = (pi, si)
    return found


def _set_sentence(paras: list[str], pi: int, si: int, text: str) -> list[str]:
    sentences = _sentences(paras[pi])
    sentences[si] = text
    return [*paras[:pi], " ".join(sentences), *paras[pi + 1 :]]


def _insert_paragraph(paras: list[str], before: int, text: str) -> list[str]:
    return [*paras[:before], text, *paras[before:]]


# --- the flaws: each returns the edited body, or None if it can't apply to this email ---

GENERIC_OPENER = "I've been following {company} for a while and love what your team is building."
EXTRA_PAIN = "Separately, retaining senior engineers is probably getting harder as you scale."
PAIN_AS_FACT = "Your onboarding is losing most new users, and your team hasn't fixed it."
VAGUE_OUTCOME = "We've helped lots of companies like yours see great results."
HYPE = "We're the game-changing, industry-leading platform, and results are guaranteed!"
OFF_ROLE = "General Counsel"  # onboarding drop-off isn't a lawyer's problem


def generic_opener(c: Case, sender: Sender) -> str | None:
    paras = _paragraphs(c.email.body)
    if len(paras) < 3:
        return None
    opener = GENERIC_OPENER.format(company=c.report.company_name)
    return "\n\n".join(_set_sentence(paras, 1, 0, opener))


def extra_pain_point(c: Case, sender: Sender) -> str | None:
    paras = _paragraphs(c.email.body)
    loc = _outcome_location(paras, sender)
    return None if loc is None else "\n\n".join(_insert_paragraph(paras, loc[0], EXTRA_PAIN))


def pain_as_fact(c: Case, sender: Sender) -> str | None:
    paras = _paragraphs(c.email.body)
    loc = _outcome_location(paras, sender)
    return None if loc is None else "\n\n".join(_insert_paragraph(paras, loc[0], PAIN_AS_FACT))


def changed_outcome_number(c: Case, sender: Sender) -> str | None:
    paras = _paragraphs(c.email.body)
    loc = _outcome_location(paras, sender)
    if loc is None:
        return None
    numbers = set(_INT.findall(" ".join(sender.outcomes)))
    sentence = _sentences(paras[loc[0]])[loc[1]]
    for m in _INT.finditer(sentence):
        if m.group() in numbers:
            changed = sentence[: m.start()] + str(int(m.group()) * 2 + 7) + sentence[m.end() :]
            return "\n\n".join(_set_sentence(paras, *loc, changed))
    return None


def vague_outcome(c: Case, sender: Sender) -> str | None:
    paras = _paragraphs(c.email.body)
    loc = _outcome_location(paras, sender)
    return None if loc is None else "\n\n".join(_set_sentence(paras, *loc, VAGUE_OUTCOME))


def salesy_hype(c: Case, sender: Sender) -> str | None:
    paras = _paragraphs(c.email.body)
    ask = _ask_index(paras, sender)
    return None if ask is None else "\n\n".join(_insert_paragraph(paras, ask, HYPE))


@dataclass(frozen=True)
class Flaw:
    name: str
    target: str  # the CRITERIA key that must fail
    edit_body: Callable[[Case, Sender], str | None] | None = None
    new_role: str | None = None  # flaws can change the recipient instead of the text


FLAWS = [
    Flaw("generic_opener", "specific_opener", generic_opener),
    Flaw("extra_pain_point", "single_pain_point", extra_pain_point),
    Flaw("pain_as_fact", "pain_as_hypothesis", pain_as_fact),
    Flaw("changed_outcome_number", "one_sender_outcome", changed_outcome_number),
    Flaw("vague_outcome", "one_sender_outcome", vague_outcome),
    Flaw("wrong_role", "relevant_to_role", new_role=OFF_ROLE),
    Flaw("salesy_hype", "human_tone", salesy_hype),
]
assert all(f.target in CRITERIA for f in FLAWS)


def apply_flaw(original: Case, flaw: Flaw, sender: Sender) -> Case | None:
    """The flawed copy, or None if the flaw doesn't apply (or would break the word limit:
    the writer can't produce a 120+ word email, so judging one would test nothing real)."""
    flawed = replace(original, flaw=flaw.name, target=flaw.target)
    if flaw.new_role:
        lead = original.lead.model_copy(update={"person_role": flaw.new_role})
        flawed = replace(flawed, lead=lead)
    if flaw.edit_body:
        body = flaw.edit_body(original, sender)
        if body is None or body == original.email.body:
            return None
        try:
            email = Email(subject=original.email.subject, body=body,
                          used_fact_ids=original.email.used_fact_ids)  # fmt: skip
        except ValidationError:
            return None
        flawed = replace(flawed, email=email)
    return flawed


def load_cases(records: list[dict[str, Any]]) -> list[Case]:
    """Originals from a saved fixture (`pipeline_smoke --save` format)."""
    return [
        Case(
            domain=r["lead"]["domain"],
            lead=Lead.model_validate(r["lead"]),
            report=ResearchReport.model_validate(r["report"]),
            pain_point=PainPoint.model_validate(r["pain_point"]),
            email=Email.model_validate(r["email"]),
        )
        for r in records
        if r.get("email") and r.get("report") and r.get("pain_point")
    ]


def build_cases(
    originals: list[Case], sender: Sender, flaws: list[Flaw] = FLAWS
) -> tuple[list[Case], list[str]]:
    """Originals plus every applicable flawed copy, and notes on flaws that didn't apply."""
    cases, skipped = list(originals), []
    for o in originals:
        for f in flaws:
            if (c := apply_flaw(o, f, sender)) is None:
                skipped.append(f"{o.domain}/{f.name}")
            else:
                cases.append(c)
    return cases, skipped


# --- scoring ---


@dataclass
class Judged:
    case: Case
    runs: list[dict[str, bool]] = field(default_factory=list)  # one verdict dict per repeat
    errors: int = 0


def _rate(xs: list[bool]) -> float | None:
    return round(mean(xs), 3) if xs else None


def summarize(judged: list[Judged]) -> dict[str, Any]:
    originals = [j for j in judged if j.case.flaw is None]
    flawed = [j for j in judged if j.case.flaw is not None]

    original_pass = {
        c: _rate([run[c] for j in originals for run in j.runs if c in run]) for c in CRITERIA
    }
    catch: dict[str, Any] = {}
    for name in dict.fromkeys(j.case.flaw for j in flawed):
        group = [j for j in flawed if j.case.flaw == name]
        target = group[0].case.target
        verdicts = [not run[target] for j in group for run in j.runs if target in run]
        catch[name] = {"target": target, "cases": len(group), "catch_rate": _rate(verdicts)}

    # Flip = the same (email, criterion) got both a pass and a fail across repeats.
    flips: dict[str, list[bool]] = {c: [] for c in CRITERIA}
    for j in judged:
        if len(j.runs) < 2:
            continue
        for c in CRITERIA:
            vs = {run[c] for run in j.runs if c in run}
            flips[c].append(len(vs) > 1)
    all_flips = [f for fs in flips.values() for f in fs]

    return {
        "originals": len(originals),
        "flawed_copies": len(flawed),
        "judge_errors": sum(j.errors for j in judged),
        "original_pass_rate": original_pass,
        "catch_rate": catch,
        "mean_catch_rate": _rate(
            [not run[j.case.target] for j in flawed for run in j.runs if j.case.target in run]
        ),
        "flip_rate": {c: _rate(fs) for c, fs in flips.items()} if all_flips else None,
        "overall_flip_rate": _rate(all_flips),
    }
