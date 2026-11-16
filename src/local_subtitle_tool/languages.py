"""Language directions: which language is transcribed and which one the subtitles use.

Internally a decision keeps the corrected source text in ``ja_fixed`` and the
translation in ``ko`` (names from the original Japanese-to-Korean design); for the
Korean-to-Japanese direction ``ja_fixed`` holds Korean and ``ko`` holds Japanese.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

JAPANESE_RESIDUE = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿]")
HANGUL_RESIDUE = re.compile(r"[ᄀ-ᇿㄱ-ㆎ가-힣]")


@dataclass(frozen=True, slots=True)
class Direction:
    key: str
    source: str  # Whisper language code
    target: str
    source_name: str
    target_name: str
    label: str
    # Text of the *source* language that must not remain in a translation.
    residue: re.Pattern[str]


DIRECTIONS: dict[str, Direction] = {
    "ja-ko": Direction(
        key="ja-ko",
        source="ja",
        target="ko",
        source_name="일본어",
        target_name="한국어",
        label="일본어 → 한국어",
        residue=JAPANESE_RESIDUE,
    ),
    "ko-ja": Direction(
        key="ko-ja",
        source="ko",
        target="ja",
        source_name="한국어",
        target_name="일본어",
        label="한국어 → 일본어",
        residue=HANGUL_RESIDUE,
    ),
}
DEFAULT_DIRECTION = "ja-ko"


def direction(key: str | None) -> Direction:
    return DIRECTIONS.get(str(key or DEFAULT_DIRECTION), DIRECTIONS[DEFAULT_DIRECTION])
