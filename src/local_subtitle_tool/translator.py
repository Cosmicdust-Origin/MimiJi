from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .codex_events import USAGE_FIELDS
from .core import (
    JobConfig,
    JobManifest,
    JobPaths,
    JobStatus,
    atomic_write_json,
    atomic_write_text,
    load_json,
    normalize_translation_profile,
    translation_profile,
)
from .languages import direction as get_direction
from .policy import should_drop_segment
from .providers import (
    ProviderCancelled,
    ProviderError,
    ProviderFailureKind,
    TranslationProvider,
    get_provider,
    provider_user_message,
)

ProgressCallback = Callable[[str], None]
ProgressValueCallback = Callable[[float | None, str], None]


class TranslationCancelled(RuntimeError):
    pass


class TranslationPartialError(RuntimeError):
    """Batches are held (refused or repeatedly invalid); the final SRT must not be built."""

    def __init__(self, refused_batch_count: int, refused_raw_ids: list[int]) -> None:
        super().__init__(
            f"번역이 보류된 배치가 {refused_batch_count}개 있습니다. "
            f"영향받은 raw_id는 {refused_raw_ids}입니다."
        )
        self.refused_batch_count = refused_batch_count
        self.refused_raw_ids = refused_raw_ids


class TranslationReviewRequired(RuntimeError):
    """Some utterances are still marked review after automatic re-translation."""

    def __init__(self, review_raw_ids: list[int]) -> None:
        super().__init__(
            f"자동 보강 번역 후에도 검토가 필요한 항목이 {len(review_raw_ids)}개 남았습니다. "
            f"raw_id: {review_raw_ids}"
        )
        self.review_raw_ids = review_raw_ids


class UnverifiableTranslationBatch(RuntimeError):
    """A legacy batch cannot be proven to belong to its recorded execution profile."""

    def __init__(self, batch_indexes: list[int]) -> None:
        normalized = sorted({int(index) for index in batch_indexes})
        numbers = [index + 1 for index in normalized]
        super().__init__(
            "기존 번역 배치의 실행 프로필과 지문을 검증할 수 없습니다. "
            f"배치 {numbers}을 새 설정으로 다시 번역할지 선택하십시오."
        )
        self.batch_indexes = normalized


TRANSLATION_POLICY_VERSION = "direct-ja-ko-v4-compact"
# Response protocol. Batch caches and holds carry it; batches from an older protocol
# are re-translated (never reported as unverifiable), holds survive the upgrade.
TRANSLATION_PROTOCOL = "compact-v1"
REVIEW_RETRY_POLICY_VERSION = "review-retry-v4"
MAX_TRANSIENT_ATTEMPTS = 3
MAX_INVALID_OUTPUT_ATTEMPTS = 2
TRANSIENT_BACKOFF_SECONDS = (5.0, 20.0)
MAX_CONSECUTIVE_HELD_BATCHES = 3
REVIEW_RETRY_BATCH_SIZE = 8
REVIEW_RETRY_MIN_CONTEXT = 24
REVIEW_SINGLE_MIN_CONTEXT = 40
VALID_ACTIONS = frozenset({"translated", "omitted", "review"})
HELD_FAILURE_KINDS = frozenset(
    {ProviderFailureKind.CONTENT_REFUSAL.value, ProviderFailureKind.INVALID_OUTPUT.value}
)
# Low ASR confidence: the model must write an explicit Japanese correction first.
LOW_CONFIDENCE_LOGPROB = -0.7
LOW_CONFIDENCE_NO_SPEECH = 0.4
LOW_CONFIDENCE_COMPRESSION = 2.4

OMIT_CODES: dict[str, str] = {
    "moan": "신음·탄식",
    "breath": "호흡",
    "laugh": "웃음",
    "noise": "비발화 소리",
    "hallucination": "무음 환각·상투어",
    "endcard": "엔드카드 상투어",
}
# Codes that claim "no words were spoken": checked against the raw text.
VOCAL_OMIT_CODES = frozenset({"moan", "breath", "laugh", "noise"})
_VOCAL_CHARACTERS = set(
    "あぁいぃうぅえぇおぉんっーはふへほひハフヘホヒアァイィウゥエェオォンッ"
    "ゃゅょャュョ～〜…・、。,.!?！？ 　♡♥"
    # Korean moans, breaths and laughter
    "아앗앙어엇엉으응음읏읍하학핫흐흑흣헉헐후휴흡ㅎㅋㅠㅜ~"
)
_RESIDUE_PATTERN = re.compile(r"[぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿]")

_ID_LIST = {"type": "array", "items": {"type": "integer"}}
TRANSLATION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"raw_id": {"type": "integer"}, "ko": {"type": "string"}},
                "required": ["raw_id", "ko"],
            },
        },
        "ja_fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"raw_id": {"type": "integer"}, "ja": {"type": "string"}},
                "required": ["raw_id", "ja"],
            },
        },
        "omitted": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "raw_id": {"type": "integer"},
                    "code": {"type": "string", "enum": sorted(OMIT_CODES)},
                },
                "required": ["raw_id", "code"],
            },
        },
        "review": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "raw_id": {"type": "integer"},
                    "ko": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["raw_id", "ko", "note"],
            },
        },
        "merge_with_previous": _ID_LIST,
        "register_summary": {"type": "string"},
    },
    "required": [
        "translations",
        "ja_fixes",
        "omitted",
        "review",
        "merge_with_previous",
        "register_summary",
    ],
}


def _mirror_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The Korean-to-Japanese response schema: translations carry ``ja``, fixes ``ko``."""

    text = json.dumps(schema, ensure_ascii=False)
    text = text.replace('"ja_fixes"', '"@FIXES@"').replace('"ja"', '"@SRC@"')
    text = text.replace('"ko"', '"ja"').replace('"@SRC@"', '"ko"')
    text = text.replace('"@FIXES@"', '"ko_fixes"')
    return json.loads(text)


TRANSLATION_SCHEMA_KO_JA: dict[str, Any] = _mirror_schema(TRANSLATION_SCHEMA)


def _schema_for(language_pair: str) -> dict[str, Any]:
    return TRANSLATION_SCHEMA_KO_JA if language_pair == "ko-ja" else TRANSLATION_SCHEMA


SYSTEM_INSTRUCTIONS = "\n".join(
    [
        "귀하는 일본어 음성 인식 결과를 보정하고 한국어 자막으로 직접 번역하는 전문 번역자입니다.",
        "",
        "다음 원칙을 반드시 준수하십시오.",
        "1. target_raw_ids의 모든 raw_id를 translations, omitted, review 중 정확히 한 곳에만 "
        "한 번씩 넣으십시오. 문맥용 항목은 반환하지 마십시오.",
        "2. translations에는 실제 발화를 자연스러운 한국어로 직접 번역한 ko만 적으십시오.",
        "3. 음성 인식이 틀렸다고 판단해 일본어를 고친 줄은 ja_fixes에 보정한 일본어를 적으십시오. "
        "ja_fix_required_raw_ids에 있는 raw_id는 번역한다면 반드시 ja_fixes에 보정문"
        "(고칠 것이 없으면 원문 그대로)을 먼저 적으십시오. 그 외에는 고친 줄만 적습니다.",
        "4. 단어 치환, 조사·어미 치환, 이전 번역 재사용, 일부 단어만 남기기, "
        "말줄임표로 가리기, 요약을 금지합니다. ko에 일본어 가나나 한자를 남기지 마십시오.",
        "5. 화자 관계와 높임말을 문맥에서 추론하여 경어, 반말, 호칭, 감정의 강도를 "
        "일관되게 보존하십시오. previous_register_summary가 있으면 이어서 따르십시오.",
        "6. 짧더라도 말이 담긴 줄(대답, 감탄, 거절, 요청, 고통·놀람의 표현 등)은 번역하십시오. "
        "짧다는 이유만으로 omitted에 넣지 마십시오.",
        "7. omitted에는 실제 말이 없는 줄만 code와 함께 넣으십시오: moan(신음·탄식), "
        "breath(호흡), laugh(웃음), noise(비발화 소리), hallucination(음성 근거 없는 상투어·환각), "
        "endcard(엔드카드 상투어). 단어가 들어 있는 줄은 번역하거나 review에 넣으십시오.",
        "8. おやすみ, ご視聴ありがとうございました, ありがとうございました는 음성 근거가 "
        "불확실하면 hallucination으로 생략하거나 review로 넣으십시오. "
        "근거 없는 '잘 자'를 만들지 마십시오.",
        "9. 확신할 수 없는 줄은 review에 가장 가능성 높은 ko와 구체적인 note를 적으십시오. "
        "ko에 '(검토)', '[검토]', '대사 확인 필요' 같은 표식을 넣지 마십시오.",
        "10. 인접한 줄이 한 문장 또는 한 자막 큐로 이어져야 하면 뒤쪽 줄의 raw_id를 "
        "merge_with_previous에 넣으십시오.",
        "11. register_summary에는 이 구간의 화자 관계, 서로의 호칭과 말투(존댓말·반말)를 "
        "1~3문장으로 요약하십시오. 다음 구간 번역의 일관성에 사용됩니다.",
    ]
)

SYSTEM_INSTRUCTIONS_KO_JA = "\n".join(
    [
        "귀하는 한국어 음성 인식 결과를 보정하고 일본어 자막으로 직접 번역하는 전문 번역자입니다.",
        "",
        "다음 원칙을 반드시 준수하십시오.",
        "1. target_raw_ids의 모든 raw_id를 translations, omitted, review 중 정확히 한 곳에만 "
        "한 번씩 넣으십시오. 문맥용 항목은 반환하지 마십시오.",
        "2. translations에는 실제 발화를 자연스러운 일본어로 직접 번역한 ja만 적으십시오.",
        "3. 음성 인식이 틀렸다고 판단해 한국어를 고친 줄은 ko_fixes에 보정한 한국어를 적으십시오. "
        "ko_fix_required_raw_ids에 있는 raw_id는 번역한다면 반드시 ko_fixes에 보정문"
        "(고칠 것이 없으면 원문 그대로)을 먼저 적으십시오. 그 외에는 고친 줄만 적습니다.",
        "4. 단어 치환, 이전 번역 재사용, 일부 단어만 남기기, 말줄임표로 가리기, 요약을 "
        "금지합니다. ja에 한글을 남기지 마십시오. 고유명사는 일본어 표기(가타카나 등)로 "
        "옮기십시오.",
        "5. 화자 관계와 높임말을 문맥에서 추론하여 です・ます체와 반말(タメ口), 경어, 호칭"
        "(さん・くん・ちゃん・様 등), 감정의 강도를 일관되게 보존하십시오. "
        "previous_register_summary가 있으면 이어서 따르십시오.",
        "6. 짧더라도 말이 담긴 줄(대답, 감탄, 거절, 요청, 고통·놀람의 표현 등)은 번역하십시오. "
        "짧다는 이유만으로 omitted에 넣지 마십시오.",
        "7. omitted에는 실제 말이 없는 줄만 code와 함께 넣으십시오: moan(신음·탄식), "
        "breath(호흡), laugh(웃음), noise(비발화 소리), hallucination(음성 근거 없는 상투어·환각), "
        "endcard(엔드카드 상투어). 단어가 들어 있는 줄은 번역하거나 review에 넣으십시오.",
        "8. '시청해 주셔서 감사합니다', '구독과 좋아요' 같은 영상 끝 인사는 음성 근거가 "
        "불확실하면 hallucination으로 생략하거나 review로 넣으십시오.",
        "9. 확신할 수 없는 줄은 review에 가장 가능성 높은 ja와 구체적인 note를 적으십시오. "
        "ja에 '(要確認)', '(검토)' 같은 표식을 넣지 마십시오.",
        "10. 인접한 줄이 한 문장 또는 한 자막 큐로 이어져야 하면 뒤쪽 줄의 raw_id를 "
        "merge_with_previous에 넣으십시오.",
        "11. register_summary에는 이 구간의 화자 관계, 서로의 호칭과 말투(경어·반말)를 "
        "1~3문장으로 요약하십시오. 다음 구간 번역의 일관성에 사용됩니다.",
    ]
)

REVIEW_RETRY_INSTRUCTIONS = "\n".join(
    [
        "",
        "추가 지시: 이번 요청의 target_raw_ids는 이전 번역에서 review로 보류된 항목입니다.",
        "1. previous_review에 이전 판단과 사유가 있습니다. 넓힌 문맥을 근거로 처음부터 "
        "다시 판단하십시오.",
        "2. 실제 발화로 판단되면 translations로 직접 번역하십시오.",
        "3. 말이 없는 신음·탄식·호흡이나 음성 근거 없는 상투어로 판단되면 "
        "omitted에 code와 함께 넣으십시오.",
        "4. 넓힌 문맥으로도 판단할 수 없을 때만 review에 남기고 남은 불확실성을 note에 적으십시오.",
        "5. 이 항목들은 merge_with_previous에 넣지 마십시오.",
    ]
)


# Whisper writes laughter as a symbol rather than a sound.
LAUGH_NOTATIONS = frozenset(
    {"笑", "(笑)", "（笑）", "w", "ｗ", "www", "ｗｗｗ"}
    | {"ㅋㅋ", "ㅋㅋㅋ", "ㅎㅎ", "ㅎㅎㅎ", "(웃음)"}
)
# A Whisper repetition loop this extreme is never real dialogue (e.g. 怪我のうううう… x100,
# compression 24.8): the model spent 8+ minutes reasoning about one such line.
EXTREME_COMPRESSION = 6.0
EXTREME_CHARACTER_RUN = 15
_CHARACTER_RUN = re.compile(rf"(.)\1{{{EXTREME_CHARACTER_RUN - 1},}}")


def _extreme_repetition(segment: dict[str, Any]) -> bool:
    try:
        if float(segment.get("compression_ratio", 0.0)) >= EXTREME_COMPRESSION:
            return True
    except (TypeError, ValueError):
        pass
    return bool(_CHARACTER_RUN.search(str(segment.get("text", ""))))


def _is_vocalization(text: str) -> bool:
    """True when the raw text has no words: only moans, breaths and punctuation."""

    stripped = "".join(text.split())
    return bool(stripped) and all(character in _VOCAL_CHARACTERS for character in stripped)


def _low_confidence(segment: dict[str, Any]) -> bool:
    def number(key: str, default: float) -> float:
        try:
            return float(segment.get(key, default))
        except (TypeError, ValueError):
            return default

    if _is_vocalization(str(segment.get("text", ""))):
        return False
    return (
        number("avg_logprob", 0.0) < LOW_CONFIDENCE_LOGPROB
        or number("no_speech_prob", 0.0) >= LOW_CONFIDENCE_NO_SPEECH
        or number("compression_ratio", 0.0) > LOW_CONFIDENCE_COMPRESSION
    )


def _log(callback: ProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _auto_omit(segment: dict[str, Any]) -> bool:
    return should_drop_segment(segment)


def _segment_view(segment: dict[str, Any]) -> dict[str, Any]:
    view = {
        "raw_id": int(segment["id"]),
        "start": round(float(segment["start"]), 3),
        "end": round(float(segment["end"]), 3),
        "text": str(segment.get("text", "")).strip(),
        "no_speech_prob": round(float(segment.get("no_speech_prob", 0.0)), 4),
        "avg_logprob": round(float(segment.get("avg_logprob", 0.0)), 4),
    }
    for key in ("speaker", "speaker_id", "speaker_label"):
        if segment.get(key) is not None:
            view[key] = str(segment[key])
    return view


def _make_prompt(
    *,
    code: str,
    all_segments: list[dict[str, Any]],
    target_ids: list[int],
    context_size: int,
    previous_review: dict[int, dict[str, Any]] | None = None,
    register_summary: str | None = None,
    direction: str = "ja-ko",
) -> str:
    reverse = direction == "ko-ja"
    index_by_id = {int(item["id"]): index for index, item in enumerate(all_segments)}
    target_start = index_by_id[target_ids[0]]
    target_end = index_by_id[target_ids[-1]] + 1
    left = max(0, target_start - context_size)
    right = min(len(all_segments), target_end + context_size)
    context = [_segment_view(item) for item in all_segments[left:right]]
    by_id = {int(item["id"]): item for item in all_segments}
    payload: dict[str, Any] = {
        "work_code": code,
        "target_raw_ids": target_ids,
        ("ko_fix_required_raw_ids" if reverse else "ja_fix_required_raw_ids"): [
            raw_id for raw_id in target_ids if raw_id in by_id and _low_confidence(by_id[raw_id])
        ],
        "context_segments": context,
        "note": (
            "context_segments에는 문맥 전용 항목이 포함될 수 있습니다. "
            "target_raw_ids만 응답하십시오."
        ),
    }
    if register_summary:
        payload["previous_register_summary"] = register_summary
    instructions = SYSTEM_INSTRUCTIONS_KO_JA if reverse else SYSTEM_INSTRUCTIONS
    if previous_review is not None:
        instructions += "\n" + REVIEW_RETRY_INSTRUCTIONS
        payload["previous_review"] = [
            {
                "raw_id": raw_id,
                ("ko_fixed" if reverse else "ja_fixed"): str(item.get("ja_fixed") or ""),
                ("ja" if reverse else "ko"): str(item.get("ko") or ""),
                "reason": str(item.get("reason") or ""),
            }
            for raw_id, item in sorted(previous_review.items())
        ]
    return instructions + "\n입력 데이터:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _validate_items(payload: dict[str, Any], expected_ids: list[int]) -> list[dict[str, Any]]:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("응답의 items가 배열이 아닙니다.")
    actual_ids: list[int] = []
    normalized: list[dict[str, Any]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            raise ValueError("응답 항목이 객체가 아닙니다.")
        item = dict(raw_item)
        if isinstance(item["raw_id"], bool) or not isinstance(item["raw_id"], int):
            raise ValueError(f"raw_id는 정수여야 합니다: {item['raw_id']!r}")
        raw_id = int(item["raw_id"])
        actual_ids.append(raw_id)
        action = str(item["action"])
        if action not in VALID_ACTIONS:
            raise ValueError(f"raw_id {raw_id}의 action 값이 올바르지 않습니다: {action}")
        ja_fixed = str(item.get("ja_fixed", "")).strip()
        ko = str(item.get("ko", "")).strip()
        reason = item.get("reason")
        if action == "translated" and (not ja_fixed or not ko):
            raise ValueError(f"raw_id {raw_id}의 직접 번역문이 비어 있습니다.")
        if action in {"omitted", "review"} and not str(reason or "").strip():
            raise ValueError(f"raw_id {raw_id}의 {action} 사유가 비어 있습니다.")
        item.update(
            {
                "raw_id": raw_id,
                "ja_fixed": ja_fixed,
                "ko": ko,
                "action": action,
                "merge_with_previous": item.get("merge_with_previous") is True,
                "reason": str(reason).strip() if reason is not None else None,
                "register_note": str(item["register_note"]).strip()
                if item.get("register_note")
                else None,
            }
        )
        normalized.append(item)
    if actual_ids != expected_ids:
        missing = sorted(set(expected_ids) - set(actual_ids))
        unexpected = sorted(set(actual_ids) - set(expected_ids))
        duplicates = sorted({item for item in actual_ids if actual_ids.count(item) > 1})
        raise ValueError(
            f"raw_id 매핑이 일치하지 않습니다. missing={missing}, "
            f"unexpected={unexpected}, duplicates={duplicates}"
        )
    return normalized


def _batch_fingerprint(
    config: JobConfig,
    *,
    prompt: str,
    target_ids: list[int],
    stage: str = "batch",
) -> str:
    source = {
        "provider": config.provider,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "speed_mode": config.speed_mode,
        "policy_version": TRANSLATION_POLICY_VERSION,
        "schema": _schema_for(config.language_pair),
        "target_ids": target_ids,
        "prompt": prompt,
    }
    if config.language_pair != "ja-ko":
        source["direction"] = config.language_pair
    if stage != "batch":
        source["stage"] = stage
        source["review_policy_version"] = REVIEW_RETRY_POLICY_VERSION
    canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _config_with_profile(config: JobConfig, profile: dict[str, str]) -> JobConfig:
    return replace(
        config,
        provider=profile["provider"],
        model=profile["model"],
        reasoning_effort=profile["reasoning_effort"],
        speed_mode=profile["speed_mode"],
    )


def _cache_profile(cached: dict[str, Any]) -> dict[str, str] | None:
    profile = normalize_translation_profile(cached.get("execution_profile"))
    if profile is not None:
        return profile
    return normalize_translation_profile(cached)


def _processed_at_from_cache(path: Path, cached: dict[str, Any]) -> str:
    if cached.get("processed_at"):
        return str(cached["processed_at"])
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


def _annotate_provenance(
    items: list[dict[str, Any]],
    *,
    profile: dict[str, str],
    processing_kind: str,
    processed_at: str,
) -> list[dict[str, Any]]:
    for item in items:
        item["execution_profile"] = dict(profile)
        item["processing_kind"] = processing_kind
        item["processed_at"] = processed_at
    return items


_EVENT_LOCK = threading.Lock()

# Work time counts wall-clock time while at least one request is running, so requests
# that run at the same time are not added up (테스트 영상 C showed 31 min for a 15-min job).
_CLOCK_LOCK = threading.Lock()
_CLOCKS: dict[int, list[float]] = {}


@contextmanager
def _counting_active_time(manifest: JobManifest) -> Iterator[None]:
    key = id(manifest)
    with _CLOCK_LOCK:
        state = _CLOCKS.setdefault(key, [0, 0.0])
        if state[0] == 0:
            state[1] = time.monotonic()
        state[0] += 1
    try:
        yield
    finally:
        elapsed = None
        with _CLOCK_LOCK:
            state[0] -= 1
            if state[0] == 0:
                elapsed = time.monotonic() - state[1]
                _CLOCKS.pop(key, None)
        if elapsed is not None:
            manifest.add_active_seconds(elapsed)

# One review re-request may not hold the job for minutes; an unresolved line goes to
# the review screen, where the user can accept, edit or delete it.
REVIEW_REQUEST_TIMEOUT_SECONDS = 150

# Share of the translation stage's progress bar: batches first, then review rounds.
BATCH_PROGRESS_SHARE = 0.8
REVIEW_ROUND_SHARES = ((0.8, 0.92), (0.92, 0.99))


class _MonotonicProgress:
    """Never lets the translation progress bar move backwards (parallel lanes, rounds)."""

    def __init__(self, callback: ProgressValueCallback | None) -> None:
        self.callback = callback
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self, fraction: float | None, message: str) -> None:
        if self.callback is None:
            return
        with self.lock:
            if fraction is not None:
                self.value = max(self.value, min(1.0, fraction))
            value = self.value
        self.callback(value, message)


def _append_provider_event(path: Path, event: dict[str, Any]) -> None:
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _EVENT_LOCK:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write_text(path, existing + line)


class _LinkedEvent(threading.Event):
    """Stop flag for parallel lanes: set by a failing lane or by the user's cancel."""

    def __init__(self, parent: threading.Event | None) -> None:
        super().__init__()
        self._parent = parent

    def is_set(self) -> bool:
        return super().is_set() or bool(self._parent is not None and self._parent.is_set())

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = 0.2 if deadline is None else min(0.2, deadline - time.monotonic())
            if remaining <= 0:
                return self.is_set()
            super().wait(remaining)
        return True


def _unlocked(lock: Any, function: Callable[..., Any], **kwargs: Any) -> Any:
    """Run a provider request without holding the shared-state lock."""

    lock.release()
    try:
        return function(**kwargs)
    finally:
        lock.acquire()


# Lanes grow with the work so every batch goes out in one round (a batch takes 3-5 min
# almost regardless of size; 테스트 영상 B's 741 lines took two rounds and 11.5 min with
# three lanes). Six concurrent Codex requests cover works up to 6 x 240 lines.
MAX_TRANSLATION_LANES = 6


def _lane_count(total: int, minimum: int, max_size: int) -> int:
    needed = -(-total // max(1, max_size)) if total else 1
    return max(1, min(MAX_TRANSLATION_LANES, max(minimum, needed)))


def _even_batch_ranges(total: int, max_size: int, lanes: int) -> list[tuple[int, int]]:
    """Split ``total`` lines into equal batches that fill every lane.

    The batch count is a multiple of the lane count, so parallel lanes finish together
    (a 240 + 63 split left one lane idle for minutes), and no batch exceeds max_size.
    """

    if total <= 0:
        return []
    lanes = max(1, lanes)
    size = max(1, max_size)
    count = min(total, lanes * -(-total // (lanes * size)))
    base, extra = divmod(total, count)
    ranges: list[tuple[int, int]] = []
    start = 0
    for index in range(count):
        end = start + base + (1 if index < extra else 0)
        ranges.append((start, end))
        start = end
    return ranges


def _batch_ranges(
    paths: JobPaths,
    manifest: JobManifest,
    config: JobConfig,
    translatable_ids: list[int],
    lanes: int,
) -> list[tuple[int, int]]:
    """The job's batch plan, fixed on first use so resumed runs reuse their checkpoints."""

    key = hashlib.sha256(json.dumps(translatable_ids).encode("utf-8")).hexdigest()
    stored = manifest.stats.get("translation_batch_plan")
    total = len(translatable_ids)
    if isinstance(stored, dict) and stored.get("key") == key:
        ranges = [(int(a), int(b)) for a, b in stored.get("ranges", [])]
        if ranges and ranges[0][0] == 0 and ranges[-1][1] == total:
            return ranges
    if any(paths.batches_dir.glob("batch_[0-9][0-9][0-9][0-9].json")):
        # Started before 0.8.6: keep the fixed-size plan its checkpoints were made with.
        ranges = [
            (start, min(start + config.batch_size, total))
            for start in range(0, total, config.batch_size)
        ]
    else:
        ranges = _even_batch_ranges(total, config.batch_size, lanes)
    with manifest.lock:
        manifest.stats["translation_batch_plan"] = {
            "key": key,
            "ranges": [list(item) for item in ranges],
        }
        manifest.save()
    return ranges


def _split_contiguous(items: list[Any], parts: int, weights: list[int]) -> list[list[Any]]:
    """Split into at most ``parts`` contiguous chunks of roughly equal total weight."""

    remaining = sum(weights)
    chunks: list[list[Any]] = [[]]
    load = 0
    for index, (item, weight) in enumerate(zip(items, weights, strict=True)):
        lanes_left = parts - len(chunks)
        items_left = len(items) - index
        target = remaining / (lanes_left + 1)
        if chunks[-1] and lanes_left > 0 and (load + weight > target or items_left <= lanes_left):
            chunks.append([])
            remaining -= load
            load = 0
        chunks[-1].append(item)
        load += weight
    return chunks


def _run_parallel(jobs: list[Callable[[], None]], stop: threading.Event | None) -> None:
    """Run jobs in threads; the first real failure stops the others and is re-raised."""

    if len(jobs) == 1:
        jobs[0]()
        return
    errors: list[BaseException] = []

    def wrap(job: Callable[[], None]) -> None:
        try:
            job()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller thread
            errors.append(exc)
            if stop is not None:
                stop.set()

    threads = [
        threading.Thread(target=wrap, args=(job,), name=f"mimiji-lane-{index}", daemon=True)
        for index, job in enumerate(jobs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        cancelled = (ProviderCancelled, TranslationCancelled)
        raise next((exc for exc in errors if not isinstance(exc, cancelled)), errors[0])


def _load_refused_batches(path: Path) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    data = load_json(path)
    batches = data.get("batches", []) if isinstance(data, dict) else []
    if not isinstance(batches, list):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for item in batches:
        if isinstance(item, dict) and "batch_index" in item:
            result[int(item["batch_index"])] = dict(item)
    return result


def _save_refused_batches(path: Path, batches: dict[int, dict[str, Any]]) -> None:
    atomic_write_json(
        path,
        {
            "schema_version": 1,
            "batches": [batches[index] for index in sorted(batches)],
        },
    )


def _provider_event_base(
    config: JobConfig,
    *,
    batch_index: int | str,
    attempt: int,
    target_ids: list[int],
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "batch_index": batch_index,
        "attempt": attempt,
        "target_raw_ids": target_ids,
        "provider": config.provider,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "speed_mode": config.speed_mode,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }


def _wait_or_cancel(seconds: float, cancel_event: threading.Event | None) -> None:
    if cancel_event is None:
        time.sleep(seconds)
        return
    if cancel_event.wait(seconds):
        raise ProviderCancelled("사용자 요청으로 번역을 중지했습니다.")


MAX_REPAIR_ROUNDS = 2
# Rotate a persistent session before its context grows too large; every resumed turn
# re-sends the whole conversation (input tokens are cumulative).
MAX_SESSION_TURNS = 8
MAX_SESSION_INPUT_TOKENS = 350_000

REPAIR_INSTRUCTIONS = "\n".join(
    [
        "",
        "추가 지시: 직전 응답의 구조 검증에서 다음 문제가 발견되었습니다.",
        "{problems}",
        "이번 응답에는 target_raw_ids에 있는 항목만 정확히 한 번씩 반환하십시오.",
        "이미 정상으로 받은 다른 raw_id는 반환하지 마십시오. 번역 원칙은 동일합니다.",
    ]
)


class SessionManager:
    """Persistent provider conversation for one translation run (Codex `exec resume`).

    The active session is bound to the execution profile; it is retired on refusal,
    profile change, failed resume, explicit held-batch retry, or rotation limits.
    """

    def __init__(self, manifest: JobManifest, config: JobConfig, lane: int = 0) -> None:
        self.manifest = manifest
        self.config = config
        # Lane 0 is the classic single session; parallel lanes keep their own session.
        self.lane = lane

    @property
    def active(self) -> dict[str, Any] | None:
        sessions = self.manifest.translation_sessions
        if self.lane == 0:
            return sessions.get("active")
        return (sessions.get("lanes") or {}).get(str(self.lane))

    def _set_active(self, record: dict[str, Any] | None) -> None:
        sessions = self.manifest.translation_sessions
        if self.lane == 0:
            sessions["active"] = record
        elif record is None:
            (sessions.get("lanes") or {}).pop(str(self.lane), None)
        else:
            sessions.setdefault("lanes", {})[str(self.lane)] = record

    def retire(self, reason: str) -> None:
        with self.manifest.lock:
            active = self.active
            if not active:
                return
            record = dict(active)
            record.update(
                {"state": "retired", "retired_reason": reason, "retired_at": _now()}
            )
            self.manifest.translation_sessions.setdefault("retired", []).append(record)
            self._set_active(None)
            self.manifest.save()

    def retire_session(self, session_id: str | None, reason: str, label: int | str) -> None:
        """Retire a session by id, recording it even if it never completed a turn."""

        with self.manifest.lock:
            self._retire_session_locked(session_id, reason, label)

    def _retire_session_locked(
        self, session_id: str | None, reason: str, label: int | str
    ) -> None:
        active = self.active
        if active and (not session_id or active.get("session_id") == session_id):
            self.retire(reason)
            return
        if not session_id:
            return
        now = _now()
        self.manifest.translation_sessions.setdefault("retired", []).append(
            {
                "session_id": session_id,
                "provider": self.config.provider,
                "execution_profile": translation_profile(self.config),
                "created_at": now,
                "last_used_at": now,
                "turns": 1,
                "batches": [str(label)],
                "last_input_tokens": None,
                "state": "retired",
                "retired_reason": reason,
                "retired_at": now,
            }
        )
        self.manifest.save()

    def resume_id(self) -> str | None:
        active = self.active
        if not active:
            return None
        if (
            active.get("provider") != self.config.provider
            or active.get("execution_profile") != translation_profile(self.config)
        ):
            self.retire("profile_changed")
            return None
        if int(active.get("turns") or 0) >= MAX_SESSION_TURNS or int(
            active.get("last_input_tokens") or 0
        ) >= MAX_SESSION_INPUT_TOKENS:
            self.retire("rotated")
            return None
        return str(active["session_id"])

    def per_turn_usage(self, session_id: str | None, metrics: dict[str, Any]) -> None:
        """Convert cumulative session usage into this turn's usage, in place.

        `codex exec resume` reports token usage summed over the whole thread
        (verified: turn 2 input = turn 1 + turn 2). Cumulative values are kept under
        `cumulative_*`; the plain fields become per-turn deltas.
        """

        active = self.active
        previous = (
            (active or {}).get("usage_totals")
            if active and active.get("session_id") == session_id
            else None
        ) or {}
        totals: dict[str, int] = {}
        for name in USAGE_FIELDS:
            value = metrics.get(name)
            if not isinstance(value, int):
                continue
            totals[name] = value
            metrics[f"cumulative_{name}"] = value
            before = previous.get(name)
            if isinstance(before, int) and value >= before:
                metrics[name] = value - before
        metrics["_usage_totals"] = totals

    def record(self, session_id: str | None, label: int | str, metrics: dict[str, Any]) -> None:
        if not session_id:
            return
        with self.manifest.lock:
            self._record_locked(session_id, label, metrics)

    def _record_locked(self, session_id: str, label: int | str, metrics: dict[str, Any]) -> None:
        active = self.active
        if not active or active.get("session_id") != session_id:
            if active:
                self.retire("replaced")
            active = {
                "session_id": session_id,
                "provider": self.config.provider,
                "execution_profile": translation_profile(self.config),
                "created_at": _now(),
                "turns": 0,
                "batches": [],
                "state": "active",
            }
            self._set_active(active)
        active["last_used_at"] = _now()
        active["turns"] = int(active.get("turns") or 0) + 1
        if str(label) not in active["batches"]:
            active["batches"].append(str(label))
        tokens = metrics.get("input_tokens")
        # Per-turn input = the whole conversation re-sent this turn (context size).
        active["last_input_tokens"] = int(tokens) if isinstance(tokens, int) else None
        totals = metrics.pop("_usage_totals", None)
        if totals:
            active["usage_totals"] = totals
        self.manifest.save()


@dataclass(slots=True)
class _RunContext:
    sessions: SessionManager | None
    phase: Callable[[str], None] | None
    segments_by_id: dict[int, dict[str, Any]] | None = None
    register_summary: str | None = None
    held_streak: int = 0
    direction: str = "ja-ko"


_ACTIVE_RUN: ContextVar[_RunContext | None] = ContextVar("mimiji_active_run", default=None)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _check_item(raw_item: object) -> dict[str, Any]:
    """Validate and normalize one response item (same rules as _validate_items)."""

    if not isinstance(raw_item, dict):
        raise ValueError("응답 항목이 객체가 아닙니다.")
    item = dict(raw_item)
    if isinstance(item.get("raw_id"), bool) or not isinstance(item.get("raw_id"), int):
        raise ValueError(f"raw_id는 정수여야 합니다: {item.get('raw_id')!r}")
    raw_id = int(item["raw_id"])
    action = str(item.get("action"))
    if action not in VALID_ACTIONS:
        raise ValueError(f"raw_id {raw_id}의 action 값이 올바르지 않습니다: {action}")
    ja_fixed = str(item.get("ja_fixed", "")).strip()
    ko = str(item.get("ko", "")).strip()
    reason = item.get("reason")
    if action == "translated" and (not ja_fixed or not ko):
        raise ValueError(f"raw_id {raw_id}의 직접 번역문이 비어 있습니다.")
    if action in {"omitted", "review"} and not str(reason or "").strip():
        raise ValueError(f"raw_id {raw_id}의 {action} 사유가 비어 있습니다.")
    item.update(
        {
            "raw_id": raw_id,
            "ja_fixed": ja_fixed,
            "ko": ko,
            "action": action,
            "merge_with_previous": item.get("merge_with_previous") is True,
            "reason": str(reason).strip() if reason is not None else None,
            "register_note": str(item["register_note"]).strip()
            if item.get("register_note")
            else None,
        }
    )
    return item


def _canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Korean-to-Japanese response keys mapped to the internal (source, target) names."""

    def rows(key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    result = dict(payload)
    result["translations"] = [
        {"raw_id": item.get("raw_id"), "ko": item.get("ja")} for item in rows("translations")
    ]
    result["ja_fixes"] = [
        {"raw_id": item.get("raw_id"), "ja": item.get("ko")} for item in rows("ko_fixes")
    ]
    result["review"] = [
        {"raw_id": item.get("raw_id"), "ko": item.get("ja"), "note": item.get("note")}
        for item in rows("review")
    ]
    result.pop("ko_fixes", None)
    return result


def _expand_compact(
    payload: dict[str, Any], expected_ids: list[int]
) -> tuple[dict[str, Any], set[int], list[str]]:
    """Turn a compact response into internal per-raw_id items.

    Returns the internal payload, raw_ids that must be re-requested (suspicious
    omission, missing required ja_fix, Japanese residue in ko) and problem texts.
    """

    context = _ACTIVE_RUN.get()
    segments = (context.segments_by_id if context else None) or {}
    pair = context.direction if context else "ja-ko"
    if pair == "ko-ja":
        payload = _canonical_payload(payload)
    residue = get_direction(pair).residue
    problems: list[str] = []
    rejected: set[int] = set()

    def entries(key: str) -> list[dict[str, Any]]:
        value = payload.get(key)
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    ja_fixes = {
        int(item["raw_id"]): str(item.get("ja") or "").strip()
        for item in entries("ja_fixes")
        if isinstance(item.get("raw_id"), int)
    }
    merges = {
        int(value)
        for value in payload.get("merge_with_previous") or []
        if isinstance(value, int) and not isinstance(value, bool)
    }
    summary = payload.get("register_summary")
    if context is not None and isinstance(summary, str) and summary.strip():
        context.register_summary = summary.strip()[:800]

    def raw_text(raw_id: int) -> str:
        return str(segments.get(raw_id, {}).get("text", "")).strip()

    items: list[dict[str, Any]] = []
    for entry in entries("translations"):
        raw_id = entry.get("raw_id")
        if not isinstance(raw_id, int):
            items.append(entry)
            continue
        ko = str(entry.get("ko") or "").strip()
        segment = segments.get(raw_id)
        if segment is not None and _low_confidence(segment) and raw_id not in ja_fixes:
            rejected.add(raw_id)
            problems.append(
                f"raw_id {raw_id}는 인식 신뢰도가 낮아 "
                f"{'ko_fixes' if pair == 'ko-ja' else 'ja_fixes'}에 보정문이 필요합니다."
            )
        if residue.search(ko):
            rejected.add(raw_id)
            problems.append(
                f"raw_id {raw_id}의 ja에 한글이 남아 있습니다."
                if pair == "ko-ja"
                else f"raw_id {raw_id}의 ko에 일본어 가나나 한자가 남아 있습니다."
            )
        items.append(
            {
                "raw_id": raw_id,
                "ja_fixed": ja_fixes.get(raw_id) or raw_text(raw_id) or ko,
                "ko": ko,
                "action": "translated",
                "merge_with_previous": raw_id in merges,
                "reason": None,
                "register_note": None,
            }
        )
    for entry in entries("omitted"):
        raw_id = entry.get("raw_id")
        code = str(entry.get("code") or "")
        if isinstance(raw_id, int):
            segment = segments.get(raw_id, {})
            text = raw_text(raw_id)
            suspicious = False
            if code in VOCAL_OMIT_CODES:
                try:
                    vocal_no_speech = float(segment.get("no_speech_prob", 0.0))
                except (TypeError, ValueError):
                    vocal_no_speech = 0.0
                suspicious = bool(segment) and not (
                    _is_vocalization(text)
                    or text in LAUGH_NOTATIONS
                    or vocal_no_speech >= 0.5
                )
            elif code in {"hallucination", "endcard"}:
                try:
                    no_speech = float(segment.get("no_speech_prob", 0.0))
                except (TypeError, ValueError):
                    no_speech = 0.0
                suspicious = bool(segment) and not (
                    should_drop_segment(segment)
                    or no_speech >= 0.5
                    or _is_generic(text)
                    or _is_repetition_loop(segments, raw_id)
                )
            else:
                suspicious = True
            if suspicious:
                rejected.add(raw_id)
                problems.append(
                    f"raw_id {raw_id}는 실제 말이 있을 수 있어 {code or '사유 없음'}으로 생략할 수 "
                    "없습니다. 번역하거나 review에 넣으십시오."
                )
        items.append(
            {
                "raw_id": raw_id,
                "ja_fixed": raw_text(raw_id) if isinstance(raw_id, int) else "",
                "ko": "",
                "action": "omitted",
                "merge_with_previous": False,
                "reason": OMIT_CODES.get(code, code) or None,
                "register_note": None,
                "omit_code": code,
            }
        )
    for entry in entries("review"):
        raw_id = entry.get("raw_id")
        items.append(
            {
                "raw_id": raw_id,
                "ja_fixed": (ja_fixes.get(raw_id) if isinstance(raw_id, int) else None)
                or (raw_text(raw_id) if isinstance(raw_id, int) else ""),
                "ko": str(entry.get("ko") or "").strip(),
                "action": "review",
                "merge_with_previous": False,
                "reason": str(entry.get("note") or "").strip() or None,
                "register_note": None,
            }
        )
    order = {raw_id: index for index, raw_id in enumerate(expected_ids)}
    items.sort(key=lambda item: order.get(item.get("raw_id"), len(order)))
    return {"items": items}, rejected, problems


def _is_repetition_loop(segments: dict[int, dict[str, Any]], raw_id: int) -> bool:
    """Whisper repetition loop: very high compression, or 3+ identical lines in a row."""

    segment = segments.get(raw_id) or {}
    try:
        if float(segment.get("compression_ratio", 0.0)) > LOW_CONFIDENCE_COMPRESSION:
            return True
    except (TypeError, ValueError):
        pass
    text = str(segment.get("text", "")).strip()
    if not text:
        return False
    run = 1
    for step in (-1, 1):
        neighbor = raw_id + step
        while str(segments.get(neighbor, {}).get("text", "")).strip() == text:
            run += 1
            neighbor += step
    return run >= 3


def _is_generic(text: str) -> bool:
    return should_drop_segment({"text": text, "no_speech_prob": 1.0, "end": 0.0})


def _partition_items(
    payload: dict[str, Any], expected_ids: list[int]
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """Split a response into individually valid items and a list of problems.

    Duplicates are invalid for every copy, unexpected ids are dropped and recorded,
    and incomplete items do not count as valid.
    """

    rejected: set[int] = set()
    compact_problems: list[str] = []
    if "translations" in payload:
        payload, rejected, compact_problems = _expand_compact(payload, expected_ids)
    items = payload.get("items")
    if not isinstance(items, list):
        return {}, ["응답의 items가 배열이 아닙니다."]
    expected = set(expected_ids)
    counts: dict[int, int] = {}
    for raw_item in items:
        if isinstance(raw_item, dict) and isinstance(raw_item.get("raw_id"), int):
            counts[int(raw_item["raw_id"])] = counts.get(int(raw_item["raw_id"]), 0) + 1
    valid: dict[int, dict[str, Any]] = {}
    problems: list[str] = []
    for raw_item in items:
        raw_id = raw_item.get("raw_id") if isinstance(raw_item, dict) else None
        if isinstance(raw_id, int) and not isinstance(raw_id, bool):
            if raw_id not in expected:
                problems.append(f"요청하지 않은 raw_id {raw_id}를 반환했습니다(폐기).")
                continue
            if counts.get(raw_id, 0) > 1:
                continue
        try:
            item = _check_item(raw_item)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        valid[item["raw_id"]] = item
    duplicates = sorted(
        raw_id for raw_id, count in counts.items() if count > 1 and raw_id in expected
    )
    if duplicates:
        problems.append(f"raw_id가 중복되었습니다: {duplicates}")
    for raw_id in rejected:
        valid.pop(raw_id, None)
    problems.extend(compact_problems)
    missing = [raw_id for raw_id in expected_ids if raw_id not in valid]
    if missing:
        problems.append(f"다음 raw_id의 유효한 항목이 없습니다: {missing}")
    return valid, problems


def _load_partial(path: Path | None, key: str | None) -> dict[int, dict[str, Any]]:
    if path is None or key is None or not path.is_file():
        return {}
    try:
        data = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("fingerprint") != key:
        return {}
    result: dict[int, dict[str, Any]] = {}
    for raw_item in data.get("valid_items", []):
        try:
            item = _check_item(raw_item)
        except ValueError:
            continue
        result[item["raw_id"]] = item
    return result


def _bump_metrics(manifest: JobManifest, metrics: dict[str, Any], *, repair: bool) -> None:
    with manifest.lock:
        _bump_metrics_locked(manifest, metrics, repair=repair)


def _bump_metrics_locked(
    manifest: JobManifest, metrics: dict[str, Any], *, repair: bool
) -> None:
    totals = manifest.stats.setdefault(
        "translation_metrics",
        {
            "requests": 0,
            "new_sessions": 0,
            "resumed_requests": 0,
            "ephemeral_requests": 0,
            "repair_requests": 0,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "token_usage_available": False,
            "observed_service_tier": "unavailable",
        },
    )
    totals["requests"] += 1
    mode = metrics.get("session_mode")
    key = {"new": "new_sessions", "resume": "resumed_requests"}.get(str(mode), "ephemeral_requests")
    totals[key] += 1
    if repair:
        totals["repair_requests"] += 1
    for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"):
        value = metrics.get(name)
        if isinstance(value, int):
            totals[name] += value
            totals["token_usage_available"] = True
    tier = metrics.get("observed_service_tier")
    if tier and tier != "unavailable":
        totals["observed_service_tier"] = tier


def _request_items(
    *,
    provider: TranslationProvider,
    config: JobConfig,
    manifest: JobManifest,
    paths: JobPaths,
    prompt: str,
    expected_ids: list[int],
    label: int | str,
    schema_path: Path,
    output_path: Path,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
    repair_prompt: Callable[[list[int], list[str]], str] | None = None,
    checkpoint_key: str | None = None,
    force_new_session: bool = False,
    request_timeout: int | None = None,
    max_transient_attempts: int | None = None,
) -> list[dict[str, Any]]:
    """Run one provider request with bounded retries and partial INVALID_OUTPUT repair.

    Valid items of a response are kept; only missing or invalid raw_ids are asked
    again (same session when sessions are enabled), at most MAX_REPAIR_ROUNDS times.
    Content refusals are never retried and retire the session. Raises ProviderError
    or ProviderCancelled like before.
    """

    context = _ACTIVE_RUN.get()
    sessions = context.sessions if context else None
    phase = context.phase if context else None
    partial_path = (
        paths.batches_dir / f"partial_{checkpoint_key[:16]}.json" if checkpoint_key else None
    )
    if force_new_session and sessions is not None:
        sessions.retire("held_retry")

    def notify(text: str) -> None:
        if phase is not None:
            phase(text)

    def call(text: str, ids: list[int], attempt: int, stage: str) -> dict[str, Any]:
        session_id = sessions.resume_id() if sessions is not None else None
        started = time.monotonic()
        base = {
            "stage": stage,
            "request_started_at": _now(),
        }
        try:
            with _counting_active_time(manifest):
                result = provider.run(
                    prompt=text,
                    model=config.model,
                    reasoning_effort=config.reasoning_effort,
                    speed_mode=config.speed_mode,
                    schema_path=schema_path,
                    output_path=output_path,
                    workdir=paths.state_dir,
                    cancel_event=cancel_event,
                    session_id=session_id,
                    persistent=sessions is not None,
                    on_phase=phase,
                    **({"timeout": request_timeout} if request_timeout else {}),
                )
        except ProviderCancelled:
            raise
        except ProviderError as exc:
            elapsed = time.monotonic() - started
            if sessions is not None:
                if exc.kind == ProviderFailureKind.CONTENT_REFUSAL:
                    sessions.retire_session(exc.session_id or session_id, "refused", label)
                elif session_id and exc.kind not in {
                    ProviderFailureKind.AUTH_REQUIRED,
                    ProviderFailureKind.USAGE_LIMIT,
                }:
                    sessions.retire("resume_failed")
            _bump_metrics(manifest, exc.metrics or {}, repair=stage == "repair")
            event = _provider_event_base(
                config, batch_index=label, attempt=attempt, target_ids=ids, elapsed_seconds=elapsed
            )
            event.update(base)
            event.update(
                {
                    "outcome": "failure",
                    "failure_kind": exc.kind.value,
                    "message": str(exc),
                    "returncode": exc.returncode,
                    "raw_response": exc.raw_response,
                    "stdout": exc.stdout,
                    "stderr": exc.stderr,
                    "session_id": exc.session_id or session_id,
                    "session_mode": exc.session_mode,
                    "metrics": exc.metrics,
                    "request_finished_at": _now(),
                }
            )
            _append_provider_event(paths.provider_events, event)
            raise
        elapsed = time.monotonic() - started
        if sessions is not None:
            sessions.per_turn_usage(result.session_id, result.metrics)
            sessions.record(result.session_id, label, result.metrics)
        _bump_metrics(manifest, result.metrics, repair=stage == "repair")
        event = _provider_event_base(
            config, batch_index=label, attempt=attempt, target_ids=ids, elapsed_seconds=elapsed
        )
        event.update(base)
        event.update(
            {
                "outcome": "success",
                "session_id": result.session_id,
                "session_mode": result.session_mode,
                "metrics": result.metrics,
                "request_finished_at": _now(),
            }
        )
        _append_provider_event(paths.provider_events, event)
        return result.payload

    attempt = 0
    while True:
        attempt += 1
        try:
            valid = _load_partial(partial_path, checkpoint_key)
            problems: list[str] = []
            if not valid:
                payload = call(prompt, expected_ids, attempt, "initial")
                notify("구조 검증 중")
                valid, problems = _partition_items(payload, expected_ids)
            rounds = 0
            while True:
                missing = [raw_id for raw_id in expected_ids if raw_id not in valid]
                if not missing:
                    break
                if repair_prompt is None or rounds >= MAX_REPAIR_ROUNDS or not valid:
                    raise ProviderError(
                        "번역 응답의 raw_id 매핑 또는 필드가 유효하지 않습니다: "
                        + "; ".join(problems)[:400],
                        kind=ProviderFailureKind.INVALID_OUTPUT,
                    )
                if partial_path is not None:
                    atomic_write_json(
                        partial_path,
                        {
                            "fingerprint": checkpoint_key,
                            "label": str(label),
                            "valid_items": [valid[i] for i in expected_ids if i in valid],
                            "problems": problems,
                            "updated_at": _now(),
                        },
                    )
                rounds += 1
                notify(f"누락 raw_id 부분 복구 중 ({len(missing)}개)")
                _log(progress, f"번역 요청 {label}: raw_id {missing}만 다시 요청합니다.")
                repaired = call(
                    repair_prompt(missing, problems or ["누락된 raw_id가 있습니다."]),
                    missing,
                    attempt,
                    "repair",
                )
                notify("구조 검증 중")
                fixed, problems = _partition_items(repaired, missing)
                for raw_id, item in fixed.items():
                    valid.setdefault(raw_id, item)  # never overwrite accepted items
            if partial_path is not None:
                partial_path.unlink(missing_ok=True)
            return [valid[raw_id] for raw_id in expected_ids]
        except ProviderError as exc:
            if exc.kind == ProviderFailureKind.CONTENT_REFUSAL:
                if partial_path is not None:
                    partial_path.unlink(missing_ok=True)
                raise
            retry_limit = 1
            if exc.kind == ProviderFailureKind.TRANSIENT_ERROR:
                retry_limit = max_transient_attempts or MAX_TRANSIENT_ATTEMPTS
            elif exc.kind == ProviderFailureKind.INVALID_OUTPUT:
                retry_limit = MAX_INVALID_OUTPUT_ATTEMPTS
            if attempt >= retry_limit:
                if exc.kind == ProviderFailureKind.INVALID_OUTPUT and partial_path is not None:
                    partial_path.unlink(missing_ok=True)
                raise
            _log(
                progress,
                f"번역 요청 {label}의 {exc.kind.value} 오류로 다시 시도합니다. "
                f"({attempt + 1}/{retry_limit})",
            )
            if exc.kind == ProviderFailureKind.TRANSIENT_ERROR:
                delay = TRANSIENT_BACKOFF_SECONDS[
                    min(attempt - 1, len(TRANSIENT_BACKOFF_SECONDS) - 1)
                ]
                _wait_or_cancel(delay, cancel_event)


def _make_repair_prompt(
    *,
    code: str,
    all_segments: list[dict[str, Any]],
    target_ids: list[int],
    context_size: int,
    problems: list[str],
    previous_review: dict[int, dict[str, Any]] | None = None,
    direction: str = "ja-ko",
) -> str:
    base = _make_prompt(
        code=code,
        all_segments=all_segments,
        target_ids=target_ids,
        context_size=context_size,
        previous_review=previous_review,
        direction=direction,
    )
    instructions = REPAIR_INSTRUCTIONS.replace(
        "{problems}", "\n".join(f"- {item}" for item in problems[:20])
    )
    marker = "\n입력 데이터:\n"
    head, _, tail = base.partition(marker)
    return head + "\n" + instructions + marker + tail


def _load_cached_items(
    path: Path, fingerprint: str, expected_ids: list[int]
) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    try:
        cached = load_json(path)
        if cached.get("fingerprint") != fingerprint:
            return None
        return _validate_items(cached, expected_ids)
    except (KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError):
        return None


def _load_verified_batch_items(
    path: Path,
    *,
    config: JobConfig,
    manifest: JobManifest,
    prompt: str,
    expected_ids: list[int],
    batch_index: int,
    current_fingerprint: str,
) -> tuple[list[dict[str, Any]], dict[str, str], str] | None:
    """Load a batch under the normal fingerprint rule or an explicit profile switch.

    Cross-profile reuse is only available after the user opted into it in manifest
    schema 4. Legacy batches are accepted only after recomputing their original
    fingerprint from the profile stored in the batch file.
    """

    if not path.exists():
        return None
    try:
        cached = load_json(path)
        if not isinstance(cached, dict):
            return None
        cached_fingerprint = str(cached.get("fingerprint", ""))
        if cached_fingerprint == current_fingerprint:
            profile = _cache_profile(cached) or translation_profile(config)
        else:
            resume = manifest.translation_resume
            if not resume.get("reuse_completed_batches_across_profiles"):
                return None
            profile = _cache_profile(cached)
            allowed = [
                normalized
                for item in resume.get("allowed_profiles", [])
                if (normalized := normalize_translation_profile(item)) is not None
            ]
            retranslate = {
                int(index)
                for index in resume.get("retranslate_unverified_batches", [])
            }
            if profile is None:
                if batch_index in retranslate:
                    return None
                raise UnverifiableTranslationBatch([batch_index])
            if profile not in allowed:
                return None
            recorded_config = _config_with_profile(config, profile)
            recorded_fingerprint = _batch_fingerprint(
                recorded_config,
                prompt=prompt,
                target_ids=expected_ids,
            )
            if cached_fingerprint != recorded_fingerprint:
                if batch_index in retranslate:
                    return None
                raise UnverifiableTranslationBatch([batch_index])
        items = _validate_items(cached, expected_ids)
        return items, profile, _processed_at_from_cache(path, cached)
    except UnverifiableTranslationBatch:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError, OSError):
        return None


def _cached_failure(path: Path, fingerprint: str) -> str | None:
    if not path.exists():
        return None
    try:
        cached = load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
        failed = cached.get("failed")
        return str(failed) if failed else None
    return None


def _held_review_failure_across_allowed_profiles(
    *,
    paths: JobPaths,
    config: JobConfig,
    manifest: JobManifest,
    prompt: str,
    target_ids: list[int],
    stage: str,
) -> bool:
    resume = manifest.translation_resume
    if not resume.get("reuse_completed_batches_across_profiles"):
        return False
    for item in resume.get("allowed_profiles", []):
        profile = normalize_translation_profile(item)
        if profile is None:
            continue
        recorded_config = _config_with_profile(config, profile)
        fingerprint = _batch_fingerprint(
            recorded_config,
            prompt=prompt,
            target_ids=target_ids,
            stage=stage,
        )
        path = paths.batches_dir / f"{stage}_{fingerprint[:16]}.json"
        if _cached_failure(path, fingerprint) in HELD_FAILURE_KINDS:
            return True
    return False


def _save_cached_items(
    path: Path,
    config: JobConfig,
    fingerprint: str,
    expected_ids: list[int],
    items: list[dict[str, Any]],
    *,
    stage: str = "batch",
    register_summary: str | None = None,
) -> str:
    processed_at = datetime.now(UTC).isoformat()
    atomic_write_json(
        path,
        {
            "fingerprint": fingerprint,
            "protocol": TRANSLATION_PROTOCOL,
            "register_summary": register_summary,
            "stage": stage,
            "policy_version": TRANSLATION_POLICY_VERSION,
            "provider": config.provider,
            "model": config.model,
            "reasoning_effort": config.reasoning_effort,
            "speed_mode": config.speed_mode,
            "execution_profile": translation_profile(config),
            "processed_at": processed_at,
            "target_ids": expected_ids,
            "items": items,
        },
    )
    return processed_at


def _write_translation_artifacts(
    paths: JobPaths,
    decisions: list[dict[str, Any]],
    *,
    partial: bool,
    refused_raw_ids: list[int],
    review_raw_ids: list[int] | None = None,
) -> None:
    atomic_write_json(
        paths.decisions,
        {
            "partial": partial,
            "refused_raw_ids": refused_raw_ids,
            "unresolved_review_raw_ids": review_raw_ids or [],
            "items": decisions,
        },
    )
    atomic_write_json(
        paths.ja_fix,
        {
            str(item["raw_id"]): item["ja_fixed"]
            for item in decisions
            if item["action"] == "translated"
        },
    )
    atomic_write_json(
        paths.ko_by_index,
        {
            str(item["raw_id"]): item["ko"]
            for item in decisions
            if item["action"] == "translated"
        },
    )


# --------------------------------------------------------------------------------------
# Resolutions: manual input and user-requested single retries
# --------------------------------------------------------------------------------------


# Serializes read-modify-write of resolutions.json between the GUI thread (manual
# input) and a worker thread (user-requested retry).
_RESOLUTIONS_LOCK = threading.RLock()


def load_resolutions(paths: JobPaths) -> dict[int, dict[str, Any]]:
    if not paths.resolutions.is_file():
        return {}
    try:
        data = load_json(paths.resolutions)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    items = data.get("items", {}) if isinstance(data, dict) else {}
    if not isinstance(items, dict):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for key, value in items.items():
        try:
            result[int(key)] = dict(value)
        except (TypeError, ValueError):
            continue
    return result


def _save_resolutions(paths: JobPaths, resolutions: dict[int, dict[str, Any]]) -> None:
    atomic_write_json(
        paths.resolutions,
        {
            "schema_version": 1,
            "items": {str(key): resolutions[key] for key in sorted(resolutions)},
        },
    )


def _resolution_applies(resolution: dict[str, Any], segment: dict[str, Any]) -> bool:
    """A resolution is only valid for the exact raw text it was written for."""

    return str(resolution.get("source_text", "")) == str(segment.get("text", "")).strip()


def _resolution_decision(resolution: dict[str, Any]) -> dict[str, Any]:
    action = str(resolution.get("action", "translated"))
    return {
        "raw_id": int(resolution["raw_id"]),
        "ja_fixed": str(resolution.get("ja_fixed", "")),
        "ko": str(resolution.get("ko", "")) if action == "translated" else "",
        "action": action,
        "merge_with_previous": False,
        "reason": resolution.get("reason"),
        "register_note": None,
        "source": str(resolution.get("source", "manual")),
        "execution_profile": dict(
            resolution.get("execution_profile")
            or {
                "provider": "manual",
                "model": "manual",
                "reasoning_effort": "manual",
                "speed_mode": "manual",
            }
        ),
        "processing_kind": str(resolution.get("processing_kind", "manual")),
        "processed_at": str(
            resolution.get("processed_at") or resolution.get("updated_at") or ""
        ),
    }


def _retract_published(config: JobConfig, reason: str) -> None:
    from .subtitles import retract_published_outputs

    retract_published_outputs(config, reason)


def _load_raw_segments(paths: JobPaths) -> list[dict[str, Any]]:
    if not paths.raw.exists():
        raise FileNotFoundError(f"원본 전사 JSON이 없습니다: {paths.raw}")
    raw = load_json(paths.raw)
    segments = list(raw.get("segments", []))
    for fallback_id, segment in enumerate(segments):
        segment["id"] = int(segment.get("id", fallback_id))
    segments.sort(key=lambda item: int(item["id"]))
    expected_all_ids = [int(item["id"]) for item in segments]
    if expected_all_ids != list(range(len(segments))):
        raise ValueError("원본 전사의 raw_id가 0부터 연속적이지 않습니다.")
    return segments


HELD_KIND_LABELS = {
    "CONTENT_REFUSAL": "모델 거절",
    "INVALID_OUTPUT": "형식 오류",
}


def _response_excerpt(text: object, limit: int = 160) -> str:
    """One-line excerpt of a provider response for the review screen."""

    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _held_reason(record: dict[str, Any]) -> str:
    kind = str(record.get("failure_kind") or "")
    batch = int(record.get("batch_index", -1)) + 1
    attempts = int(record.get("refusal_attempts") or 1)
    if kind == "CONTENT_REFUSAL":
        head = f"모델이 번역 배치 {batch}을 거절했습니다"
    elif kind == "INVALID_OUTPUT":
        head = f"번역 배치 {batch}에서 유효한 형식의 응답을 받지 못했습니다"
    else:
        head = f"번역 배치 {batch}을 처리하지 못했습니다({kind or '원인 불명'})"
    if attempts > 1:
        head += f" · {attempts}회째"
    excerpt = _response_excerpt(record.get("raw_response")) or _response_excerpt(
        record.get("message")
    )
    return f"{head}. 모델 응답: “{excerpt}”" if excerpt else f"{head}."


def _resolution_label(resolution: dict[str, Any] | None) -> str:
    if resolution is None:
        return ""
    if resolution.get("action") == "omitted":
        return "자막에서 삭제함"
    kind = resolution.get("processing_kind")
    if kind == "review_accept":
        return "모델 번역 채택"
    if resolution.get("source") == "manual":
        return "직접 입력 저장됨"
    return "재번역 결과 저장됨"


def pending_review_items(config: JobConfig) -> list[dict[str, Any]]:
    """Unresolved review and held items for the review screen."""

    paths = JobPaths(config)
    if not paths.decisions.is_file():
        return []
    segments = _load_raw_segments(paths)
    by_id = {int(item["id"]): item for item in segments}
    data = load_json(paths.decisions)
    decisions = {int(item["raw_id"]): item for item in data.get("items", [])}
    resolutions = load_resolutions(paths)
    pending: list[dict[str, Any]] = []
    review_ids = [int(item) for item in data.get("unresolved_review_raw_ids", [])]
    if not review_ids:
        review_ids = [
            raw_id for raw_id, item in decisions.items() if item.get("action") == "review"
        ]
    held_ids = [int(item) for item in data.get("refused_raw_ids", [])]
    held_records: dict[int, dict[str, Any]] = {}
    for record in _load_refused_batches(paths.refused_batches).values():
        for raw_id in record.get("target_raw_ids", []):
            held_records[int(raw_id)] = record
    for kind, raw_ids in (("review", review_ids), ("held", held_ids)):
        for raw_id in sorted(set(raw_ids)):
            segment = by_id.get(raw_id)
            if segment is None:
                continue
            decision = decisions.get(raw_id, {})
            resolution = resolutions.get(raw_id)
            if resolution is not None and not _resolution_applies(resolution, segment):
                resolution = None
            pending.append(
                {
                    "raw_id": raw_id,
                    "kind": kind,
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "raw_ja": str(segment.get("text", "")).strip(),
                    "speaker": segment.get("speaker"),
                    "ja_fixed": str(
                        (resolution or decision).get("ja_fixed")
                        or segment.get("text", "")
                    ).strip(),
                    "ko": str((resolution or decision).get("ko") or "").strip(),
                    "reason": (
                        _held_reason(held_records[raw_id])
                        if kind == "held" and raw_id in held_records
                        else str(decision.get("reason") or "").strip()
                    ),
                    "held_kind": HELD_KIND_LABELS.get(
                        str(held_records.get(raw_id, {}).get("failure_kind") or ""), ""
                    )
                    if kind == "held"
                    else "",
                    "resolved": resolution is not None,
                    "resolution_source": (resolution or {}).get("source"),
                    "resolution_label": _resolution_label(resolution),
                }
            )
    return pending


def _store_resolutions(config: JobConfig, entries: list[dict[str, Any]]) -> None:
    paths = JobPaths(config)
    with _RESOLUTIONS_LOCK:
        resolutions = load_resolutions(paths)
        for entry in entries:
            resolutions[int(entry["raw_id"])] = entry
        _save_resolutions(paths, resolutions)
    manifest = JobManifest.load_or_create(config)
    manifest.stats["manual_translation_count"] = sum(
        1 for item in resolutions.values() if item.get("source") == "manual"
    )
    manifest.save()


def _user_resolution(
    segment: dict[str, Any],
    *,
    action: str,
    ja_fixed: str,
    ko: str,
    reason: str | None,
    processing_kind: str,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "raw_id": int(segment["id"]),
        "action": action,
        "ja_fixed": ja_fixed,
        "ko": ko,
        "reason": reason,
        "source": "manual",
        "execution_profile": {
            "provider": "manual",
            "model": "manual",
            "reasoning_effort": "manual",
            "speed_mode": "manual",
        },
        "processing_kind": processing_kind,
        "source_text": str(segment.get("text", "")).strip(),
        "processed_at": now,
        "updated_at": now,
    }


def _korean_problem(ko_value: str, language_pair: str = "ja-ko") -> str | None:
    from .subtitles import FORBIDDEN_MARKERS

    spec = get_direction(language_pair)
    if not ko_value:
        return f"{spec.target_name} 번역이 비어 있습니다."
    if spec.residue.search(ko_value):
        return (
            "일본어 번역에 한글이 남아 있습니다."
            if language_pair == "ko-ja"
            else "한국어 번역에 일본어 가나나 한자가 남아 있습니다."
        )
    if any(marker in ko_value for marker in FORBIDDEN_MARKERS):
        return "한국어 번역에 검토 표식을 넣을 수 없습니다."
    return None


def save_manual_translation(
    config: JobConfig, raw_id: int, ja_fixed: str, ko: str
) -> dict[str, Any]:
    paths = JobPaths(config)
    by_id = {int(item["id"]): item for item in _load_raw_segments(paths)}
    if raw_id not in by_id:
        raise ValueError(f"raw_id {raw_id}를 원본 전사에서 찾을 수 없습니다.")
    ja_value = " ".join(ja_fixed.split()) or str(by_id[raw_id].get("text", "")).strip()
    ko_value = " ".join(ko.split())
    problem = _korean_problem(ko_value, config.language_pair)
    if problem:
        raise ValueError(problem)
    resolution = _user_resolution(
        by_id[raw_id],
        action="translated",
        ja_fixed=ja_value,
        ko=ko_value,
        reason=None,
        processing_kind="manual",
    )
    _store_resolutions(config, [resolution])
    return resolution


def omit_review_items(config: JobConfig, raw_ids: list[int]) -> int:
    """User decision: drop these lines from the subtitles (moans, noise, misheard lines)."""

    by_id = {int(item["id"]): item for item in _load_raw_segments(JobPaths(config))}
    entries = [
        _user_resolution(
            by_id[raw_id],
            action="omitted",
            ja_fixed=str(by_id[raw_id].get("text", "")).strip(),
            ko="",
            reason="검토 화면에서 자막에서 제외",
            processing_kind="review_omit",
        )
        for raw_id in raw_ids
        if raw_id in by_id
    ]
    if entries:
        _store_resolutions(config, entries)
    return len(entries)


def accept_model_translations(
    config: JobConfig, raw_ids: list[int] | None = None
) -> tuple[int, list[int]]:
    """User decision: use the model's tentative translation as final.

    Only review items that have a usable Korean line are accepted; held lines (no
    translation) and lines with Japanese residue are returned as skipped.
    """

    pending = {
        item["raw_id"]: item
        for item in pending_review_items(config)
        if not item["resolved"] and (raw_ids is None or item["raw_id"] in raw_ids)
    }
    by_id = {int(item["id"]): item for item in _load_raw_segments(JobPaths(config))}
    entries: list[dict[str, Any]] = []
    skipped: list[int] = []
    for raw_id, item in sorted(pending.items()):
        ko_value = " ".join(str(item.get("ko") or "").split())
        problem = _korean_problem(ko_value, config.language_pair)
        if item["kind"] != "review" or problem or raw_id not in by_id:
            skipped.append(raw_id)
            continue
        entries.append(
            _user_resolution(
                by_id[raw_id],
                action="translated",
                ja_fixed=str(item.get("ja_fixed") or by_id[raw_id].get("text", "")).strip(),
                ko=ko_value,
                reason=None,
                processing_kind="review_accept",
            )
        )
    if entries:
        _store_resolutions(config, entries)
    return len(entries), skipped


def retry_review_item(
    config: JobConfig,
    raw_id: int,
    *,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Re-translate one unresolved item once in a fresh session at the user's request.

    The result is stored as a resolution only when the model resolves the item
    (translated or omitted). A remaining review is returned without being stored.
    """

    paths = JobPaths(config)
    segments = _load_raw_segments(paths)
    by_id = {int(item["id"]): item for item in segments}
    if raw_id not in by_id:
        raise ValueError(f"raw_id {raw_id}를 원본 전사에서 찾을 수 없습니다.")
    previous: dict[str, Any] = {}
    if paths.decisions.is_file():
        for item in load_json(paths.decisions).get("items", []):
            if int(item["raw_id"]) == raw_id:
                previous = item
                break
    manifest = JobManifest.load_or_create(config)
    provider = get_provider(config.provider)
    schema_path = paths.state_dir / "translation_schema.json"
    atomic_write_json(schema_path, _schema_for(config.language_pair))
    prompt = _make_prompt(
        code=config.code,
        direction=config.language_pair,
        all_segments=segments,
        target_ids=[raw_id],
        context_size=max(config.context_size * 5, REVIEW_SINGLE_MIN_CONTEXT),
        previous_review={
            raw_id: previous
            or {"reason": "이전 배치가 보류되어 이 항목의 번역 결과가 없습니다."}
        },
    )
    retry_token = _ACTIVE_RUN.set(
        _RunContext(
            sessions=None, phase=None, segments_by_id=by_id, direction=config.language_pair
        )
    )
    try:
        items = _request_items(
            provider=provider,
            config=config,
            manifest=manifest,
            paths=paths,
            prompt=prompt,
            expected_ids=[raw_id],
            label=f"user_retry_{raw_id}",
            schema_path=schema_path,
            output_path=paths.batches_dir / f"user_retry_{raw_id:06d}_provider_output.json",
            cancel_event=cancel_event,
            progress=progress,
        )
    finally:
        _ACTIVE_RUN.reset(retry_token)
    item = items[0]
    if item["action"] in {"translated", "omitted"}:
        with _RESOLUTIONS_LOCK:
            resolutions = load_resolutions(paths)
            if resolutions.get(raw_id, {}).get("source") == "manual":
                # Never overwrite what the user typed while this request was running.
                return item
            resolutions[raw_id] = {
                "raw_id": raw_id,
                "action": item["action"],
                "ja_fixed": item["ja_fixed"],
                "ko": item["ko"] if item["action"] == "translated" else "",
                "reason": item.get("reason"),
                "source": "review_retry_user",
                "execution_profile": translation_profile(config),
                "processing_kind": "review_retry_user",
                "source_text": str(by_id[raw_id].get("text", "")).strip(),
                "processed_at": datetime.now(UTC).isoformat(),
            }
            resolutions[raw_id]["updated_at"] = resolutions[raw_id]["processed_at"]
            _save_resolutions(paths, resolutions)
    return item


# --------------------------------------------------------------------------------------
# Automatic review recovery
# --------------------------------------------------------------------------------------


def _recover_review_items(
    *,
    provider: TranslationProvider,
    config: JobConfig,
    manifest: JobManifest,
    paths: JobPaths,
    segments: list[dict[str, Any]],
    decisions_by_id: dict[int, dict[str, Any]],
    schema_path: Path,
    cancel_event: threading.Event | None,
    progress: ProgressCallback | None,
    progress_value: ProgressValueCallback | None,
    lanes: list[_RunContext] | None = None,
    skip_ids: set[int] | None = None,
) -> None:
    """Re-request review items with wider context, first in small groups, then singly.

    With several ``lanes`` the groups of a round are sent concurrently, one provider
    session per lane.

    Resolved items replace the review decision in ``decisions_by_id``. Refusals,
    invalid output and other per-request failures leave the item as review; only
    authentication and usage-limit failures stop the job.
    """

    rounds = (
        ("review_small", REVIEW_RETRY_BATCH_SIZE, REVIEW_RETRY_MIN_CONTEXT, 3),
        ("review_single", 1, REVIEW_SINGLE_MIN_CONTEXT, 5),
    )
    # Items whose re-request was refused (or produced invalid output) are not sent
    # again automatically, neither in a later round nor on a later run.
    excluded: set[int] = set()
    review_lock = threading.Lock()
    first_round_count = 0
    for round_index, (stage, group_size, minimum_context, context_factor) in enumerate(rounds):
        review_ids = sorted(
            raw_id
            for raw_id, item in decisions_by_id.items()
            if item["action"] == "review" and raw_id not in excluded
        )
        if not review_ids:
            return
        context_size = max(config.context_size * context_factor, minimum_context)
        groups = [
            review_ids[index : index + group_size]
            for index in range(0, len(review_ids), group_size)
        ]
        round_start, round_end = REVIEW_ROUND_SHARES[min(round_index, len(REVIEW_ROUND_SHARES) - 1)]
        if round_index == 0:
            first_round_count = len(review_ids)
            round_label = "검토 보강 1차(묶음)"
            announce = (
                f"모델이 확신하지 못한 검토 항목 {len(review_ids)}개를 넓힌 문맥으로 "
                f"다시 번역합니다 (묶음 {len(groups)}회)."
            )
        else:
            round_label = "검토 보강 2차(한 줄씩)"
            announce = (
                f"1차 보강 후에도 확정되지 않은 {len(review_ids)}개"
                + (f"(1차 대상 {first_round_count}개 중)" if first_round_count else "")
                + "를 한 줄씩 더 넓은 문맥으로 다시 확인합니다."
            )
        _log(progress, announce)
        if progress_value:
            progress_value(round_start, f"{round_label} 시작 · {announce}")
        done_groups = [0]

        def handle(
            position: int,
            group: list[int],
            stage: str = stage,
            context_size: int = context_size,
            total: int = len(groups),
            round_label: str = round_label,
            round_start: float = round_start,
            round_end: float = round_end,
            done_groups: list[int] = done_groups,
        ) -> None:
            if cancel_event and cancel_event.is_set():
                raise TranslationCancelled("사용자 요청으로 번역을 중지했습니다.")
            if skip_ids and set(group) <= skip_ids:
                return  # every line of this group was already decided by the user
            previous = {raw_id: decisions_by_id[raw_id] for raw_id in group}
            prompt = _make_prompt(
                code=config.code,
                direction=config.language_pair,
                all_segments=segments,
                target_ids=group,
                context_size=context_size,
                previous_review=previous,
            )
            fingerprint = _batch_fingerprint(
                config, prompt=prompt, target_ids=group, stage=stage
            )
            cache_path = paths.batches_dir / f"{stage}_{fingerprint[:16]}.json"
            failed_kind = _cached_failure(cache_path, fingerprint)
            if failed_kind is not None or _held_review_failure_across_allowed_profiles(
                paths=paths,
                config=config,
                manifest=manifest,
                prompt=prompt,
                target_ids=group,
                stage=stage,
            ):
                excluded.update(group)
                return
            items = _load_cached_items(cache_path, fingerprint, group)
            if items is None:
                # standard and fast differ only in service tier (same model and
                # reasoning), so a review result made at the other speed is reused.
                for speed in ("standard", "fast"):
                    if speed == config.speed_mode:
                        continue
                    other = _batch_fingerprint(
                        replace(config, speed_mode=speed),
                        prompt=prompt,
                        target_ids=group,
                        stage=stage,
                    )
                    other_path = paths.batches_dir / f"{stage}_{other[:16]}.json"
                    items = _load_cached_items(other_path, other, group)
                    if items is not None:
                        cache_path = other_path
                        break
            if items is None:
                try:
                    items = _unlocked(
                        review_lock,
                        _request_items,
                        provider=provider,
                        config=config,
                        manifest=manifest,
                        paths=paths,
                        prompt=prompt,
                        expected_ids=group,
                        label=f"{stage}_{group[0]}",
                        schema_path=schema_path,
                        output_path=paths.batches_dir
                        / f"{stage}_{fingerprint[:16]}_provider_output.json",
                        cancel_event=cancel_event,
                        progress=progress,
                        repair_prompt=lambda ids, problems, previous=previous, size=context_size: (  # noqa: E501
                            _make_repair_prompt(
                                code=config.code,
                                direction=config.language_pair,
                                all_segments=segments,
                                target_ids=ids,
                                context_size=size,
                                problems=problems,
                                previous_review={
                                    raw_id: previous[raw_id]
                                    for raw_id in ids
                                    if raw_id in previous
                                },
                            )
                        ),
                        checkpoint_key=fingerprint,
                        request_timeout=REVIEW_REQUEST_TIMEOUT_SECONDS,
                        max_transient_attempts=1,
                    )
                except ProviderError as exc:
                    if exc.kind in {
                        ProviderFailureKind.AUTH_REQUIRED,
                        ProviderFailureKind.USAGE_LIMIT,
                    }:
                        raise
                    if exc.kind == ProviderFailureKind.TRANSIENT_ERROR:
                        # Timed out or stalled: leave it for the review screen instead of
                        # spending more minutes on the same lines in the next round.
                        excluded.update(group)
                    _log(
                        progress,
                        f"검토 항목 raw_id {group}의 보강 번역이 {exc.kind.value}로 "
                        "실패하여 검토 상태를 유지합니다.",
                    )
                    if exc.kind.value in HELD_FAILURE_KINDS:
                        excluded.update(group)
                        atomic_write_json(
                            cache_path,
                            {
                                "fingerprint": fingerprint,
                                "stage": stage,
                                "failed": exc.kind.value,
                                "target_ids": group,
                                "execution_profile": translation_profile(config),
                                "processed_at": datetime.now(UTC).isoformat(),
                            },
                        )
                    return
                processed_at = _save_cached_items(
                    cache_path, config, fingerprint, group, items, stage=stage
                )
                _annotate_provenance(
                    items,
                    profile=translation_profile(config),
                    processing_kind=stage,
                    processed_at=processed_at,
                )
            else:
                cached = load_json(cache_path)
                _annotate_provenance(
                    items,
                    profile=_cache_profile(cached) or translation_profile(config),
                    processing_kind=stage,
                    processed_at=_processed_at_from_cache(cache_path, cached),
                )
            for item in items:
                if item["action"] == "review":
                    # Keep the newest uncertainty reason for the next round / the UI.
                    decisions_by_id[item["raw_id"]]["reason"] = item.get("reason")
                    continue
                item["source"] = f"{config.provider}:{stage}"
                item["merge_with_previous"] = False
                decisions_by_id[int(item["raw_id"])] = item
            if progress_value:
                done_groups[0] += 1
                progress_value(
                    round_start + (round_end - round_start) * done_groups[0] / max(total, 1),
                    f"{round_label} {done_groups[0]}/{total} 완료",
                )

        pending = list(enumerate(groups))

        def worker(lane: _RunContext | None, handle=handle, pending=pending) -> Callable[[], None]:
            def job() -> None:
                token = _ACTIVE_RUN.set(lane) if lane is not None else None
                try:
                    while True:
                        with review_lock:
                            if not pending:
                                return
                            position, group = pending.pop(0)
                            handle(position, group)
                finally:
                    if token is not None:
                        _ACTIVE_RUN.reset(token)

            return job

        workers: list[_RunContext | None] = list(lanes or [None])[: max(1, len(groups))]
        _run_parallel([worker(lane) for lane in workers], cancel_event)


def translate(
    config: JobConfig,
    *,
    progress: ProgressCallback | None = None,
    progress_value: ProgressValueCallback | None = None,
    cancel_event: threading.Event | None = None,
    retry_held: bool = False,
    codex_sessions: bool = True,
    progress_phase: Callable[[str], None] | None = None,
    parallel_batches: int = 1,
) -> list[dict[str, Any]]:
    """Translate every raw segment, reusing verified batch checkpoints.

    ``parallel_batches`` > 1 splits the batches into that many contiguous lanes that
    run at the same time, each with its own provider session and register summary.

    Batches that were held (refused or repeatedly invalid) with the same fingerprint are
    not sent again unless ``retry_held`` is set by an explicit user action.
    """
    paths = JobPaths(config)
    manifest = JobManifest.load_or_create(config)
    segments = _load_raw_segments(paths)
    expected_all_ids = [int(item["id"]) for item in segments]
    segment_by_id = {int(item["id"]): item for item in segments}

    paths.batches_dir.mkdir(parents=True, exist_ok=True)
    schema_path = paths.state_dir / "translation_schema.json"
    atomic_write_json(schema_path, _schema_for(config.language_pair))
    provider = get_provider(config.provider)
    manifest.stats["current_stage"] = "translation"
    manifest.save()
    if cancel_event and cancel_event.is_set():
        manifest.set_status(JobStatus.CANCELLED)
        raise TranslationCancelled("사용자 요청으로 번역을 중지했습니다.")
    status = provider.status()
    if not status.ok:
        kind = ProviderFailureKind.AUTH_REQUIRED
        message = provider_user_message(kind, status.message)
        manifest.stats["translation_blocked_kind"] = kind.value
        manifest.set_status(JobStatus.TRANSLATION_BLOCKED, error=message)
        raise ProviderError(
            message,
            kind=kind,
            stderr=status.message,
        )

    manifest.set_status(JobStatus.TRANSLATING)
    manifest.stats.pop("translation_blocked_kind", None)
    manifest.stats.pop("unverifiable_batch_indexes", None)
    manifest.completed_batches = []
    manifest.save()
    decisions_by_id: dict[int, dict[str, Any]] = {}
    refused_batches = _load_refused_batches(paths.refused_batches)
    current_fingerprints: dict[int, str] = {}
    translatable: list[dict[str, Any]] = []
    for segment in segments:
        raw_id = int(segment["id"])
        if _auto_omit(segment):
            decisions_by_id[raw_id] = {
                "raw_id": raw_id,
                "ja_fixed": str(segment.get("text", "")).strip(),
                "ko": "",
                "action": "omitted",
                "merge_with_previous": False,
                "reason": "고신뢰 무음/클립 경계 상투어 환각 후보",
                "register_note": None,
                "source": "narrow_auto_filter",
            }
        else:
            translatable.append(segment)

    if parallel_batches > 1:  # 1 means strictly sequential (explicit choice)
        parallel_batches = _lane_count(len(translatable), parallel_batches, config.batch_size)
    batch_ranges = _batch_ranges(
        paths,
        manifest,
        config,
        [int(item["id"]) for item in translatable],
        parallel_batches,
    )
    total_batches = len(batch_ranges)
    progress_value = _MonotonicProgress(progress_value) if progress_value else None
    finished_batches = [0]

    def batch_fraction(finished: bool) -> float:
        if finished:
            finished_batches[0] += 1
        return BATCH_PROGRESS_SHARE * finished_batches[0] / max(total_batches, 1)

    if progress_value:
        progress_value(
            0.0 if total_batches else 1.0,
            f"번역 대상 {len(translatable)}개 · 전체 배치 {total_batches}개",
        )

    sessions = (
        SessionManager(manifest, config)
        if codex_sessions and getattr(provider, "supports_sessions", False)
        else None
    )
    run_context = _RunContext(
        sessions=sessions,
        phase=progress_phase,
        segments_by_id=segment_by_id,
        direction=config.language_pair,
    )
    # Lane 0 is the main context; extra lanes get their own session and summary.
    lanes = [run_context] + [
        _RunContext(
            sessions=SessionManager(manifest, config, lane=index) if sessions else None,
            phase=None,
            segments_by_id=segment_by_id,
            direction=config.language_pair,
        )
        for index in range(1, max(1, parallel_batches))
    ]
    run_cancel = _LinkedEvent(cancel_event)
    context_token = _ACTIVE_RUN.set(run_context)
    try:
        state_lock = threading.Lock()

        def process_batch(batch_index: int, start: int, lane: _RunContext) -> None:
            if run_cancel and run_cancel.is_set():
                raise TranslationCancelled("사용자 요청으로 번역을 중지했습니다.")
            end = batch_ranges[batch_index][1]
            batch_segments = translatable[start:end]
            expected_ids = [int(item["id"]) for item in batch_segments]
            batch_path = paths.batches_dir / f"batch_{batch_index:04d}.json"
            prompt = _make_prompt(
                code=config.code,
                direction=config.language_pair,
                all_segments=segments,
                target_ids=expected_ids,
                context_size=config.context_size,
            )
            fingerprint = _batch_fingerprint(
                config,
                prompt=prompt,
                target_ids=expected_ids,
            )
            current_fingerprints[batch_index] = fingerprint
            existing_refusal = refused_batches.get(batch_index)
            profile_switch = bool(
                manifest.translation_resume.get(
                    "reuse_completed_batches_across_profiles", False
                )
            )
            refusal_targets_match = (
                existing_refusal is not None
                and [int(item) for item in existing_refusal.get("target_raw_ids", [])]
                == expected_ids
            )
            refusal_source_match = (
                existing_refusal is not None
                and existing_refusal.get("source_segments")
                == [_segment_view(item) for item in batch_segments]
            )
            if (
                existing_refusal
                and existing_refusal.get("fingerprint") != fingerprint
                and not (
                    (
                        profile_switch
                        or existing_refusal.get("protocol") != TRANSLATION_PROTOCOL
                    )
                    and refusal_targets_match
                    and refusal_source_match
                )
            ):
                refused_batches.pop(batch_index, None)
                _save_refused_batches(paths.refused_batches, refused_batches)
                existing_refusal = None
            cached_result = _load_verified_batch_items(
                batch_path,
                config=config,
                manifest=manifest,
                prompt=prompt,
                expected_ids=expected_ids,
                batch_index=batch_index,
                current_fingerprint=fingerprint,
            )
            if cached_result is None and batch_path.exists():
                _log(progress, f"기존 배치 {batch_index + 1}이 유효하지 않아 다시 번역합니다.")
            if cached_result is not None:
                cached_items, cached_profile, cached_at = cached_result
                try:
                    cached_summary = load_json(batch_path).get("register_summary")
                except (OSError, ValueError, AttributeError):
                    cached_summary = None
                if isinstance(cached_summary, str) and cached_summary.strip():
                    lane.register_summary = cached_summary.strip()
                _annotate_provenance(
                    cached_items,
                    profile=cached_profile,
                    processing_kind="batch",
                    processed_at=cached_at,
                )
                for item in cached_items:
                    item["source"] = cached_profile["provider"]
                    decisions_by_id[int(item["raw_id"])] = item
                if batch_index in refused_batches:
                    refused_batches.pop(batch_index, None)
                    _save_refused_batches(paths.refused_batches, refused_batches)
                if batch_index not in manifest.completed_batches:
                    manifest.completed_batches.append(batch_index)
                    manifest.save()
                lane.held_streak = 0
                _log(progress, f"검증된 번역 배치 {batch_index + 1}을 재사용했습니다.")
                if progress_value:
                    progress_value(
                        batch_fraction(True),
                        f"번역 배치 {batch_index + 1}/{total_batches} 재사용 완료",
                    )
                return

            previous_hold = refused_batches.get(batch_index)
            if (
                not retry_held
                and previous_hold is not None
                and (
                    previous_hold.get("fingerprint") == fingerprint
                    or (
                        (
                            profile_switch
                            or previous_hold.get("protocol") != TRANSLATION_PROTOCOL
                        )
                        and [
                            int(item)
                            for item in previous_hold.get("target_raw_ids", [])
                        ]
                        == expected_ids
                        and previous_hold.get("source_segments")
                        == [_segment_view(item) for item in batch_segments]
                    )
                )
                and previous_hold.get("failure_kind") in HELD_FAILURE_KINDS
            ):
                current_fingerprints[batch_index] = str(
                    previous_hold.get("fingerprint") or fingerprint
                )
                _log(
                    progress,
                    f"번역 배치 {batch_index + 1}은 이전에 보류되어 다시 요청하지 않습니다. "
                    "검토 화면에서 다시 요청하거나 직접 입력하십시오.",
                )
                return

            send_prompt = (
                _make_prompt(
                    code=config.code,
                    direction=config.language_pair,
                    all_segments=segments,
                    target_ids=expected_ids,
                    context_size=config.context_size,
                    register_summary=lane.register_summary,
                )
                if lane.register_summary
                else prompt
            )
            request_path = paths.batches_dir / f"batch_{batch_index:04d}_request.txt"
            atomic_write_text(request_path, send_prompt)
            output_path = paths.batches_dir / f"batch_{batch_index:04d}_provider_output.json"
            _log(
                progress,
                f"번역 배치 {batch_index + 1}: raw_id "
                f"{expected_ids[0]}~{expected_ids[-1]}을 "
                f"{config.provider}/{config.model}로 처리합니다.",
            )
            if progress_value:
                progress_value(
                    batch_fraction(False),
                    f"번역 배치 {batch_index + 1}/{total_batches} 처리 중 · "
                    f"raw_id {expected_ids[0]}~{expected_ids[-1]}",
                )
            try:
                items = _unlocked(
                    state_lock,
                    _request_items,
                    provider=provider,
                    config=config,
                    manifest=manifest,
                    paths=paths,
                    prompt=send_prompt,
                    expected_ids=expected_ids,
                    label=batch_index,
                    schema_path=schema_path,
                    output_path=output_path,
                    cancel_event=run_cancel,
                    progress=progress,
                    repair_prompt=lambda ids, problems: _make_repair_prompt(
                        code=config.code,
                        direction=config.language_pair,
                        all_segments=segments,
                        target_ids=ids,
                        context_size=config.context_size,
                        problems=problems,
                    ),
                    checkpoint_key=fingerprint,
                    force_new_session=retry_held and batch_index in refused_batches,
                )
            except ProviderError as exc:
                if exc.kind.value not in HELD_FAILURE_KINDS:
                    raise
                previous = refused_batches.get(batch_index, {})
                held_attempts = (
                    int(previous.get("refusal_attempts", 0)) + 1
                    if previous.get("fingerprint") == fingerprint
                    else 1
                )
                refused_batches[batch_index] = {
                    "batch_index": batch_index,
                    "fingerprint": fingerprint,
                    "protocol": TRANSLATION_PROTOCOL,
                    "target_raw_ids": expected_ids,
                    "source_segments": [_segment_view(item) for item in batch_segments],
                    "provider": config.provider,
                    "model": config.model,
                    "reasoning_effort": config.reasoning_effort,
                    "speed_mode": config.speed_mode,
                    "failure_kind": exc.kind.value,
                    "message": str(exc),
                    "raw_response": exc.raw_response or exc.stdout or exc.stderr,
                    "refusal_attempts": held_attempts,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                _save_refused_batches(paths.refused_batches, refused_batches)
                lane.held_streak += 1
                reason = (
                    "모델에 의해 거절되어"
                    if exc.kind == ProviderFailureKind.CONTENT_REFUSAL
                    else "유효한 구조화 결과를 받지 못해"
                )
                _log(
                    progress,
                    f"번역 배치 {batch_index + 1}이 {reason} 보류했습니다. "
                    "다음 배치를 새 세션으로 계속합니다.",
                )
                if progress_value:
                    progress_value(
                        batch_fraction(True),
                        f"번역 배치 {batch_index + 1}/{total_batches} 보류",
                    )
                if lane.held_streak >= MAX_CONSECUTIVE_HELD_BATCHES:
                    raise ProviderError(
                        f"연속 {lane.held_streak}개 배치가 보류되어 번역을 중단합니다. "
                        "제공자 상태와 모델 설정을 확인하십시오.",
                        kind=ProviderFailureKind.UNKNOWN_PROVIDER_FAILURE,
                    ) from exc
                return

            lane.held_streak = 0
            processed_at = _save_cached_items(
                batch_path,
                config,
                fingerprint,
                expected_ids,
                items,
                register_summary=lane.register_summary,
            )
            _annotate_provenance(
                items,
                profile=translation_profile(config),
                processing_kind="batch",
                processed_at=processed_at,
            )
            if batch_index in refused_batches:
                refused_batches.pop(batch_index, None)
                _save_refused_batches(paths.refused_batches, refused_batches)
            for item in items:
                item["source"] = config.provider
                decisions_by_id[int(item["raw_id"])] = item
            if batch_index not in manifest.completed_batches:
                manifest.completed_batches.append(batch_index)
                manifest.save()
            if progress_value:
                progress_value(
                    batch_fraction(True),
                    f"번역 배치 {batch_index + 1}/{total_batches} 완료",
                )

        batch_plan = [(index, start) for index, (start, _) in enumerate(batch_ranges)]
        lane_count = max(1, min(parallel_batches, len(batch_plan)))

        def lane_job(lane: _RunContext, chunk: list[tuple[int, int]]) -> Callable[[], None]:
            def job() -> None:
                token = _ACTIVE_RUN.set(lane)
                try:
                    for batch_index, start in chunk:
                        with state_lock:
                            process_batch(batch_index, start, lane)
                finally:
                    _ACTIVE_RUN.reset(token)

            return job

        if lane_count > 1:
            _log(
                progress,
                f"번역 배치 {len(batch_plan)}개를 {lane_count}개 구간으로 나누어 "
                "동시에 처리합니다.",
            )
        chunks = _split_contiguous(
            batch_plan,
            lane_count,
            [end - start for start, end in batch_ranges],
        )
        _run_parallel(
            [lane_job(lanes[index], chunk) for index, chunk in enumerate(chunks)], run_cancel
        )

        active_held = {
            index: item
            for index, item in refused_batches.items()
            if item.get("failure_kind") in HELD_FAILURE_KINDS
            and item.get("fingerprint") == current_fingerprints.get(index)
        }
        _save_refused_batches(paths.refused_batches, active_held)

        # Apply manual input and user-requested retries before automatic recovery so
        # resolved items are never sent again. Manual input always wins for its raw_id
        # (while the raw text is unchanged); model-made resolutions only fill gaps.
        held_ids = {
            int(raw_id)
            for item in active_held.values()
            for raw_id in item.get("target_raw_ids", [])
        }
        for raw_id, item in list(decisions_by_id.items()):
            segment = segment_by_id.get(raw_id)
            if item["action"] == "review" and segment is not None and _extreme_repetition(segment):
                decisions_by_id[raw_id] = {
                    **item,
                    "ko": "",
                    "action": "omitted",
                    "merge_with_previous": False,
                    "reason": "반복 환각 자동 제외(같은 글자 반복·압축률 과다)",
                    "source": "repetition_filter",
                }
        resolutions = load_resolutions(paths)
        manual_count = 0
        # Review recovery groups and prompts are built from the model's own decisions,
        # so resolving a few lines by hand does not reshuffle the groups and miss every
        # cached review result.
        model_view = dict(decisions_by_id)
        user_resolved: set[int] = set()
        for raw_id, resolution in resolutions.items():
            segment = segment_by_id.get(raw_id)
            if segment is None or not _resolution_applies(resolution, segment):
                continue
            current = decisions_by_id.get(raw_id)
            unresolved = (current is None and raw_id in held_ids) or (
                current is not None and current["action"] == "review"
            )
            is_manual = resolution.get("source") == "manual"
            if unresolved or (is_manual and current is not None):
                decisions_by_id[raw_id] = _resolution_decision(resolution)
                user_resolved.add(raw_id)
                if is_manual:
                    manual_count += 1

        _recover_review_items(
            provider=provider,
            config=config,
            manifest=manifest,
            paths=paths,
            segments=segments,
            decisions_by_id=model_view,
            schema_path=schema_path,
            cancel_event=run_cancel,
            progress=progress,
            progress_value=progress_value,
            lanes=lanes,
            skip_ids=user_resolved,
        )
        for raw_id, item in model_view.items():
            if raw_id not in user_resolved:
                decisions_by_id[raw_id] = item

        remaining_held_ids = sorted(raw_id for raw_id in held_ids if raw_id not in decisions_by_id)
        review_ids = sorted(
            raw_id for raw_id, item in decisions_by_id.items() if item["action"] == "review"
        )
        decisions = [
            decisions_by_id[raw_id] for raw_id in expected_all_ids if raw_id in decisions_by_id
        ]
        partial = bool(remaining_held_ids)
        _write_translation_artifacts(
            paths,
            decisions,
            partial=partial,
            refused_raw_ids=remaining_held_ids,
            review_raw_ids=review_ids,
        )
        missing = sorted(set(expected_all_ids) - set(decisions_by_id))
        unexpected_missing = sorted(set(missing) - set(remaining_held_ids))
        if unexpected_missing:
            raise ValueError(f"보류 배치 외의 미매핑 raw_id가 남았습니다: {unexpected_missing}")
        review_count = len(review_ids)
        omitted_count = sum(item["action"] == "omitted" for item in decisions)
        manifest.stats.update(
            {
                "translated_segments": sum(item["action"] == "translated" for item in decisions),
                "review_segments": review_count,
                "omitted_segments": omitted_count,
                "unmapped_segments": len(remaining_held_ids),
                "refused_batches": len(
                    [
                        item
                        for item in active_held.values()
                        if set(item.get("target_raw_ids", [])) & set(remaining_held_ids)
                    ]
                ),
                "refused_raw_ids": remaining_held_ids,
                "manual_translation_count": manual_count,
                "provider": config.provider,
                "translation_model": config.model,
                "reasoning_effort": config.reasoning_effort,
                "speed_mode": config.speed_mode,
            }
        )
        if partial:
            partial_error = TranslationPartialError(
                int(manifest.stats["refused_batches"]),
                remaining_held_ids,
            )
            manifest.set_status(JobStatus.TRANSLATION_PARTIAL, error=str(partial_error))
            _retract_published(config, str(partial_error))
            raise partial_error
        if review_ids:
            review_error = TranslationReviewRequired(review_ids)
            manifest.set_status(JobStatus.PARTIAL_REVIEW, error=str(review_error))
            _retract_published(config, str(review_error))
            raise review_error
        manifest.stats.pop("current_stage", None)
        manifest.set_status(JobStatus.TRANSLATED)
        if progress_value:
            progress_value(1.0, "직접 번역 단계를 완료했습니다.")
        _log(progress, f"직접 번역을 저장했습니다. 제외 {omitted_count}개입니다.")
        return decisions
    except ProviderCancelled as exc:
        manifest.set_status(JobStatus.CANCELLED)
        raise TranslationCancelled(str(exc)) from exc
    except TranslationCancelled:
        manifest.set_status(JobStatus.CANCELLED)
        raise
    except (TranslationPartialError, TranslationReviewRequired):
        raise
    except UnverifiableTranslationBatch as exc:
        manifest.stats["translation_blocked_kind"] = "UNVERIFIABLE_BATCH"
        manifest.stats["unverifiable_batch_indexes"] = exc.batch_indexes
        manifest.set_status(JobStatus.TRANSLATION_BLOCKED, error=str(exc))
        raise
    except ProviderError as exc:
        manifest.stats["translation_blocked_kind"] = exc.kind.value
        manifest.set_status(JobStatus.TRANSLATION_BLOCKED, error=str(exc))
        raise
    except BaseException as exc:
        manifest.set_status(JobStatus.FAILED, error=str(exc))
        raise
    finally:
        _ACTIVE_RUN.reset(context_token)
