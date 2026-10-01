"""Text normalisation for quote matching.

Used to check that an `evidence` quote really appears on its source page. Pages and
model outputs differ in trivial ways (curly vs straight quotes, non-breaking spaces,
line breaks), so both sides are normalised before a plain substring check. Matching
stays strict otherwise: a paraphrase is NOT a quote.
"""

import re
import unicodedata

_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "…": "...",
    }
)
_WS = re.compile(r"\s+")


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_TRANSLATE)
    return _WS.sub(" ", s).strip().casefold()


def quote_in_text(quote: str, text: str) -> bool:
    q = normalise(quote)
    return bool(q) and q in normalise(text)
