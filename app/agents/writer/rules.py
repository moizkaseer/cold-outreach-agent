"""Email rules that code can check deterministically (CLAUDE.md "Writer" section).

Rules that need judgement (is the opener specific? is there exactly one pain point? is
the outcome one of the sender's?) are left to the verifier and the evals.
"""

import re

from app.schemas import Email, ResearchReport, Sender

BANNED_PHRASES = [
    "hope this finds you well",
    "hope this email finds you",
    "hope you're well",
    "hope you are well",
    "hope you're doing well",
]

# CLAUDE.md: "never mention AI/automation". Word-boundary matches, case-insensitive.
BANNED_WORDS = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|automat(e|es|ed|ing|ion|ions))\b", re.IGNORECASE
)

MARKUP = [
    (re.compile(r"\*\*|__"), "markdown bold"),
    (re.compile(r"^\s*#", re.MULTILINE), "markdown heading"),
    (re.compile(r"^\s*[-*•]\s", re.MULTILINE), "bullet list"),
    (re.compile(r"\[[^\]]+\]\([^)]+\)"), "markdown link"),
    (re.compile(r"</?[a-zA-Z][^>]*>"), "HTML tag"),
]


def lint_email(email: Email, report: ResearchReport, sender: Sender) -> list[str]:
    """Return human-readable rule violations; an empty list means the email passes."""
    problems: list[str] = []
    text = f"{email.subject}\n{email.body}"
    lowered = text.lower().replace("’", "'")

    unknown = set(email.used_fact_ids) - report.fact_ids()
    if unknown:
        problems.append(f"used_fact_ids not in the research: {sorted(unknown)}")

    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            problems.append(f'banned opener phrase: "{phrase}"')

    words = sorted({m.group(0) for m in BANNED_WORDS.finditer(text)})
    if words:
        problems.append(f"must not mention AI or automation; found: {words}")

    for pattern, name in MARKUP:
        if pattern.search(email.body):
            problems.append(f"must be plain text; found {name}")

    if sender.ask not in email.body:
        problems.append(f'must include the ask exactly as given: "{sender.ask}"')

    return problems
