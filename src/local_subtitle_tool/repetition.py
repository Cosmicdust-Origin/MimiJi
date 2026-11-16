"""Shorten mechanical repetition that Whisper produces in a loop.

Whisper sometimes writes the same short phrase over and over inside one segment
(ダメ、ダメ、ダメ、… x14), and the translation faithfully becomes "안 돼, 안 돼, …".
A run of four or more identical phrases is shortened to three, which keeps real
emphasis ("안 돼, 안 돼, 안 돼") without filling the screen.
"""

from __future__ import annotations

import re

KEEP_REPEATS = 3
MIN_REPEATS = 4
MAX_PHRASE_CHARS = 15

_PUNCTUATION = "、,，。．.!！?？…~〜"
# A phrase (may contain spaces, e.g. "안 돼") and the punctuation/space after it.
_TOKEN = re.compile(rf"([^{_PUNCTUATION}]+)([{_PUNCTUATION}\s]*)")
# The same 2-6 character unit written back to back (ダメダメダメダメ).
_JOINED_RUN = re.compile(rf"([^{_PUNCTUATION}\s]{{2,6}}?)\1{{{MIN_REPEATS - 1},}}")


def _collapse_separated(text: str) -> str:
    tokens = [(match.group(1), match.group(2)) for match in _TOKEN.finditer(text)]
    if not tokens or sum(len(a) + len(b) for a, b in tokens) != len(text):
        return text  # leading punctuation or anything unexpected: leave as is
    out: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        phrase = tokens[index][0].strip()
        end = index + 1
        while end < len(tokens) and tokens[end][0].strip() == phrase:
            end += 1
        run = end - index
        if phrase and len(phrase) <= MAX_PHRASE_CHARS and run >= MIN_REPEATS:
            kept = tokens[index : index + KEEP_REPEATS - 1] + [tokens[end - 1]]
            out.extend(kept)  # keep the last one's ending (period, question mark…)
        else:
            out.extend(tokens[index:end])
        index = end
    return "".join(a + b for a, b in out)


def collapse_repeats(text: str) -> str:
    """Shorten any phrase repeated MIN_REPEATS+ times in a row to KEEP_REPEATS."""

    if not text:
        return text
    result = _collapse_separated(text)
    return _JOINED_RUN.sub(lambda match: match.group(1) * KEEP_REPEATS, result)
