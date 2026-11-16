"""Transcription quality policy.

The policy is packaged with the app on purpose. Earlier versions optionally imported
``subtitle_pipeline_defaults`` from the surrounding workspace, which made the
checkpoint fingerprint depend on how the app was launched (source tree vs installed vs
packaged). The values below are the same as that workspace module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

POLICY_VERSION = "policy-v2-silero-per-window"
POLICY_SOURCE = f"packaged {POLICY_VERSION}"

DEFAULT_VAD_PARAMETERS: dict[str, int] = {
    "min_silence_duration_ms": 700,
    "speech_pad_ms": 150,
}

_GENERIC_PHRASES = {
    "おやすみ",
    "おやすみなさい",
    "お疲れ様でした",
    "お疲れさまでした",
    "ご視聴ありがとうございました",
    "ご覧いただきありがとうございます",
    "ありがとうございました",
    "早送り",
    "次回予告",
    "エンディング",
    # Korean phrases Whisper invents over silence or music.
    "시청해 주셔서 감사합니다",
    "시청해주셔서 감사합니다",
    "구독과 좋아요 부탁드립니다",
    "구독과 좋아요",
    "구독 좋아요 알림설정",
    "다음 영상에서 만나요",
    "MBC 뉴스 이덕영입니다",
    "끝까지 시청해주셔서 감사합니다",
}


def _normalize(text: str) -> str:
    return re.sub(r"[\s「」『』,.。!！?？…]+", "", text.strip())


_NORMALIZED_GENERIC_PHRASES = {_normalize(item) for item in _GENERIC_PHRASES}


def should_drop_segment(segment: Mapping[str, Any], text: str | None = None) -> bool:
    """Return whether a raw segment is a high-confidence generic hallucination.

    Requires a known generic phrase and either a high no-speech probability, or a
    moderately high one together with a 30-second clip-boundary timestamp. Ordinary
    quiet dialogue, including short real utterances, is kept.
    """

    candidate = _normalize(str(text if text is not None else segment.get("text", "")))
    if candidate not in _NORMALIZED_GENERIC_PHRASES:
        return False
    try:
        no_speech = float(segment.get("no_speech_prob", -1.0))
        end = float(segment.get("end", 0.0))
    except (TypeError, ValueError):
        return False
    if no_speech < 0:
        return False
    if no_speech >= 0.50:
        return True
    end_mod = end % 30.0
    return no_speech >= 0.25 and (end_mod >= 29.85 or end_mod <= 0.15)


def decode_options() -> dict[str, Any]:
    """faster-whisper decode options for one 30-second window of speech-only audio.

    Silero VAD is applied by the transcriber itself (faster-whisper ignores
    ``vad_filter`` whenever ``clip_timestamps`` is used), so it is disabled here.
    """

    return {
        "beam_size": 5,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "vad_filter": False,
        "chunk_length": 30,
        "word_timestamps": True,
    }


__all__ = [
    "DEFAULT_VAD_PARAMETERS",
    "POLICY_SOURCE",
    "POLICY_VERSION",
    "decode_options",
    "should_drop_segment",
]
