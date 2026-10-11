"""Code-checked parts of reply handling: nothing here depends on the model."""

from __future__ import annotations

import re

# Where the prospect's own words end and the quoted original begins. Our footer contains
# an unsubscribe link, so an opt-out check on the quoted part would always fire.
_QUOTE_START = re.compile(
    r"^(on .{0,200}wrote:|-{2,}\s*original message\s*-{2,}|from: .+|sent from my .+)$",
    re.IGNORECASE,
)

# Deliberately broad: a false hit only costs us one lead, a miss keeps emailing someone
# who asked us to stop.
_OPT_OUT = re.compile(
    r"\b(unsubscribe|opt(?:ing)?[\s-]?out|remove me|take me off|"
    r"stop (?:e-?mailing|contacting|messaging|sending)|"
    r"(?:do not|don'?t|never) (?:e-?mail|contact|message) me|"
    r"no (?:more|further) e-?mails?)\b",
    re.IGNORECASE,
)


def strip_quoted(text: str) -> str:
    """The prospect's own text: drops '>' lines and everything from the quoted original on."""
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if _QUOTE_START.match(stripped):
            break
        if not stripped.startswith(">"):
            kept.append(line)
    return "\n".join(kept).strip()


def asks_to_opt_out(own_text: str) -> str | None:
    """The opt-out phrase found in the prospect's own text, if any."""
    m = _OPT_OUT.search(own_text)
    return m.group(0) if m else None
