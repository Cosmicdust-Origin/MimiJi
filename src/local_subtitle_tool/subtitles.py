from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .appinfo import APP_NAME, APP_VERSION
from .core import (
    JobConfig,
    JobManifest,
    JobPaths,
    JobStatus,
    _replace_with_retry,
    atomic_write_text,
    load_json,
    normalize_translation_profile,
)
from .repetition import collapse_repeats

KANA_PATTERN = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
# Absolute rules §4 forbid Japanese kana *and* kanji left in Korean subtitles.
JAPANESE_RESIDUE_PATTERN = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
FORBIDDEN_MARKERS = ("(검토)", "[검토]", "대사 확인 필요", "번역 검토", "(要確認)", "【要確認】")
JUNK_PATTERNS = (
    re.compile(r"^[\s.…⋯]+$"),
    re.compile(r"^[\s.…⋯]+(?:라고|거든요|주세요|예요|야|니까|거예요|입니다)[.!?…\s]*$"),
    re.compile(r"[.…⋯]{2,}.*?[.…⋯]{2,}.*?(?:라고|거든요|주세요|예요|니까)"),
    re.compile(r"^(?:저|그|이|아니)[.…⋯\s]+.*?(?:라고|주세요)[.…⋯\s]*$"),
)


@dataclass(slots=True)
class Cue:
    index: int
    start: float
    end: float
    ja: str
    ko: str
    raw_ids: list[int]
    speaker: str | None = None


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    ja_cues: int
    ko_cues: int
    timing_mismatches: int
    overlaps: int
    reversals: int
    japanese_residue: int
    forbidden_markers: int
    junk_fragments: int
    goodnight_false_positives: int
    duration_overruns: int

    def to_dict(self) -> dict[str, int | bool]:
        return {
            "ok": self.ok,
            "ja_cues": self.ja_cues,
            "ko_cues": self.ko_cues,
            "timing_mismatches": self.timing_mismatches,
            "overlaps": self.overlaps,
            "reversals": self.reversals,
            "japanese_residue": self.japanese_residue,
            "forbidden_markers": self.forbidden_markers,
            "junk_fragments": self.junk_fragments,
            "goodnight_false_positives": self.goodnight_false_positives,
            "duration_overruns": self.duration_overruns,
        }


def _format_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _parse_timestamp(value: str) -> float:
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"잘못된 SRT 타임코드입니다: {value}")
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _wrap_korean(text: str, limit: int = 34) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    words = clean.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > limit and len(lines) < 1:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(lines[:2]) if len(lines) <= 2 else f"{lines[0]}\n{' '.join(lines[1:])}"


def _wrap_japanese(text: str, limit: int = 28) -> str:
    clean = re.sub(r"\s+", "", text).strip()
    if len(clean) <= limit:
        return clean
    split = _japanese_split_point(clean, limit)
    head, rest = clean[:split], clean[split:]
    if not rest:
        # Splitting at the final punctuation would leave an empty second line, which
        # writes a blank line inside the SRT cue (strict players end the cue there).
        return clean
    return f"{head}\n{rest}"


_JA_PUNCTUATION = "。！？、」』…"
_JA_PARTICLES = "をにがはでともへや"
_HIRAGANA = re.compile("[ぁ-ゟ]")


def _japanese_split_point(clean: str, limit: int) -> int:
    """Pick a line break near the middle: after punctuation, else after a particle.

    A particle only counts when the next character starts a new word (not hiragana),
    so 取って|あげる is never split but 葉を|取って is. The last character is never a
    split point, since that would leave an empty second line.
    """
    target = len(clean) / 2 if len(clean) <= limit * 2 else limit

    def best(positions: list[int]) -> int | None:
        fitting = [p for p in positions if p <= limit and len(clean) - p <= limit]
        pool = fitting or [p for p in positions if p <= limit + 6]
        return min(pool, key=lambda p: abs(p - target)) if pool else None

    punctuation = [
        i + 1 for i, char in enumerate(clean[:-1]) if char in _JA_PUNCTUATION and i >= 4
    ]
    particles = [
        i + 1
        for i, char in enumerate(clean[:-1])
        if char in _JA_PARTICLES and i >= 4 and not _HIRAGANA.match(clean[i + 1])
    ]
    for positions, reach in ((punctuation, 6), (particles, 8)):
        choice = best([p for p in positions if abs(p - target) <= reach])
        if choice is not None:
            return choice
    return min(len(clean) - 1, max(1, round(target)))


def _write_tsv(path: Path, headers: list[str], rows: list[list[Any]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    atomic_write_text(path, buffer.getvalue())


def group_translated_decisions(
    segments: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    respect_speakers: bool = False,
    language_pair: str = "ja-ko",
) -> list[dict[str, Any]]:
    """Build the exact translated raw-id groups later used as subtitle cues."""

    # Internally ``ja`` is the source-language line and ``ko`` the translation.
    reverse = language_pair == "ko-ja"
    source_joiner, target_joiner = (" ", "") if reverse else ("", " ")
    by_id = {int(item["id"]): item for item in segments}
    groups: list[dict[str, Any]] = []
    for decision in decisions:
        raw_id = int(decision["raw_id"])
        if decision["action"] != "translated":
            continue
        segment = by_id[raw_id]
        speaker = str(segment["speaker"]) if segment.get("speaker") is not None else None
        merge = (
            decision.get("merge_with_previous") is True
            and bool(groups)
            and raw_id == int(groups[-1]["raw_ids"][-1]) + 1
            and float(segment["start"]) - float(groups[-1]["end"]) <= 5.0
            and (not respect_speakers or speaker == groups[-1].get("speaker"))
        )
        if merge:
            group = groups[-1]
            group["end"] = max(float(group["end"]), float(segment["end"]))
            group["ja_parts"].append(str(decision["ja_fixed"]).strip())
            group["ko_parts"].append(str(decision["ko"]).strip())
            group["raw_ids"].append(raw_id)
        else:
            groups.append(
                {
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "ja_parts": [str(decision["ja_fixed"]).strip()],
                    "ko_parts": [str(decision["ko"]).strip()],
                    "source_parts": [
                        {"raw_id": raw_id, "text": str(decision["ja_fixed"]).strip()}
                    ],
                    "target_parts": [
                        {"raw_id": raw_id, "text": str(decision["ko"]).strip()}
                    ],
                    "raw_ids": [raw_id],
                    "speaker": speaker,
                }
            )
        if merge:
            group["source_parts"].append(
                {"raw_id": raw_id, "text": str(decision["ja_fixed"]).strip()}
            )
            group["target_parts"].append(
                {"raw_id": raw_id, "text": str(decision["ko"]).strip()}
            )

    for index, group in enumerate(groups):
        start = float(group["start"])
        end = float(group["end"])
        if end <= start:
            end = start + 0.5
        group["start"] = start
        group["end"] = end
        if index > 0:
            previous = groups[index - 1]
            if float(previous["end"]) > start:
                safe_previous_end = max(float(previous["start"]) + 0.1, start - 0.001)
                previous["end"] = safe_previous_end
                if safe_previous_end >= start:
                    group["start"] = safe_previous_end + 0.001
                    if float(group["end"]) <= float(group["start"]):
                        group["end"] = float(group["start"]) + 0.5

    for group in groups:
        group["source_text"] = collapse_repeats(
            source_joiner.join(part for part in group["ja_parts"] if part)
        )
        group["initial_target"] = collapse_repeats(
            target_joiner.join(part for part in group["ko_parts"] if part)
        ).strip()
    return groups


def _build_cues(
    segments: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    *,
    respect_speakers: bool = False,
    language_pair: str = "ja-ko",
    polishes: dict[tuple[int, ...], dict[str, Any]] | None = None,
) -> list[Cue]:
    reverse = language_pair == "ko-ja"
    wrap_source, wrap_target = (
        (_wrap_korean, _wrap_japanese) if reverse else (_wrap_japanese, _wrap_korean)
    )
    groups = group_translated_decisions(
        segments,
        decisions,
        respect_speakers=respect_speakers,
        language_pair=language_pair,
    )

    cues: list[Cue] = []
    for index, group in enumerate(groups, start=1):
        ja = str(group["source_text"])
        ko = str(group["initial_target"])
        polish = (polishes or {}).get(tuple(int(item) for item in group["raw_ids"]))
        if (
            polish
            and polish.get("status") == "accepted"
            and polish.get("language_pair") == language_pair
            and polish.get("source_text") == ja
            and polish.get("initial_target") == ko
            and str(polish.get("polished_target") or "").strip()
        ):
            ko = str(polish["polished_target"]).strip()
        cues.append(
            Cue(
                index=index,
                start=float(group["start"]),
                end=float(group["end"]),
                ja=wrap_source(ja),
                ko=wrap_target(ko),
                raw_ids=list(group["raw_ids"]),
                speaker=group.get("speaker"),
            )
        )
    return cues


def _render_srt(cues: list[Cue], language: str, *, show_speakers: bool = False) -> str:
    blocks: list[str] = []
    for cue in cues:
        text = cue.ja if language == "ja" else cue.ko
        if show_speakers and cue.speaker:
            text = f"[{cue.speaker}] {text}"
        blocks.append(
            f"{cue.index}\n{_format_timestamp(cue.start)} --> {_format_timestamp(cue.end)}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def parse_srt(path: Path) -> list[tuple[int, float, float, str]]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    cues: list[tuple[int, float, float, str]] = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.splitlines()
        if len(lines) < 3:
            raise ValueError(f"SRT 블록 형식이 잘못되었습니다: {block[:80]}")
        index = int(lines[0].strip())
        timing = re.fullmatch(r"(.+?)\s+-->\s+(.+)", lines[1].strip())
        if not timing:
            raise ValueError(f"SRT 타임코드 줄이 잘못되었습니다: {lines[1]}")
        cues.append(
            (
                index,
                _parse_timestamp(timing.group(1)),
                _parse_timestamp(timing.group(2)),
                "\n".join(lines[2:]),
            )
        )
    return cues


def validate_srt_pair(
    ja_path: Path,
    ko_path: Path,
    *,
    duration: float | None = None,
    language_pair: str = "ja-ko",
) -> ValidationResult:
    """Check a (source SRT, translated SRT) pair; ``ja``/``ko`` name source/target."""

    from .languages import direction

    reverse = language_pair == "ko-ja"
    residue_pattern = direction(language_pair).residue
    ja_cues = parse_srt(ja_path)
    ko_cues = parse_srt(ko_path)
    timing_mismatches = abs(len(ja_cues) - len(ko_cues))
    overlaps = 0
    reversals = 0
    duration_overruns = 0
    japanese_residue = 0
    forbidden_markers = 0
    junk_fragments = 0
    goodnight_false_positives = 0

    for position, cue in enumerate(ko_cues):
        index, start, end, ko_text = cue
        if position < len(ja_cues):
            ja_index, ja_start, ja_end, ja_text = ja_cues[position]
            if index != ja_index or abs(start - ja_start) > 0.001 or abs(end - ja_end) > 0.001:
                timing_mismatches += 1
            normalized_ja = re.sub(r"\s+", "", ja_text)
            if not reverse and "잘 자" in ko_text and "おやすみ" not in normalized_ja:
                goodnight_false_positives += 1
        if end <= start:
            reversals += 1
        if position and start < ko_cues[position - 1][2] - 0.001:
            overlaps += 1
        if duration is not None and end > duration + 0.05:
            duration_overruns += 1
        if residue_pattern.search(ko_text):
            japanese_residue += 1
        if any(marker in ko_text for marker in FORBIDDEN_MARKERS):
            forbidden_markers += 1
        if not reverse and any(pattern.search(ko_text.strip()) for pattern in JUNK_PATTERNS):
            junk_fragments += 1

    ok = not any(
        (
            timing_mismatches,
            overlaps,
            reversals,
            duration_overruns,
            japanese_residue,
            forbidden_markers,
            junk_fragments,
            goodnight_false_positives,
        )
    )
    return ValidationResult(
        ok=ok,
        ja_cues=len(ja_cues),
        ko_cues=len(ko_cues),
        timing_mismatches=timing_mismatches,
        overlaps=overlaps,
        reversals=reversals,
        japanese_residue=japanese_residue,
        forbidden_markers=forbidden_markers,
        junk_fragments=junk_fragments,
        goodnight_false_positives=goodnight_false_positives,
        duration_overruns=duration_overruns,
    )


def _translation_metrics_text(config: JobConfig) -> str:
    """Request/session metrics; values the CLI did not report stay '확인 불가'."""

    try:
        manifest = JobManifest.load_or_create(config)
    except (OSError, ValueError):
        return ""
    totals = manifest.stats.get("translation_metrics") or {}
    sessions = manifest.translation_sessions or {}
    if not totals:
        return ""
    session_count = len(sessions.get("retired") or []) + (1 if sessions.get("active") else 0)
    refused_sessions = sum(
        1 for item in sessions.get("retired") or [] if item.get("retired_reason") == "refused"
    )
    if totals.get("token_usage_available"):
        tokens = (
            f"입력 {totals.get('input_tokens', 0)} (캐시 {totals.get('cached_input_tokens', 0)})"
            f" / 출력 {totals.get('output_tokens', 0)}"
            f" / 추론 {totals.get('reasoning_output_tokens', 0)}"
        )
    else:
        tokens = "확인 불가"
    tier = totals.get("observed_service_tier") or "unavailable"
    requested = "fast" if config.speed_mode == "fast" else "default"
    tier_text = "확인 불가" if tier == "unavailable" else tier
    lines = [
        f"- 번역 요청 수: {totals.get('requests', 0)}"
        f" (새 세션 {totals.get('new_sessions', 0)},"
        f" 세션 재개 {totals.get('resumed_requests', 0)},"
        f" 독립 실행 {totals.get('ephemeral_requests', 0)},"
        f" 부분 복구 {totals.get('repair_requests', 0)})",
        f"- 번역 세션: {session_count}개 (거절로 폐기 {refused_sessions}개)",
        f"- 토큰 사용량: {tokens}",
        f"- 서비스 등급: 요청 {requested} / 실제 적용 {tier_text}",
    ]
    return "\n".join(lines) + "\n"


def _report_text(
    *,
    config: JobConfig,
    raw: dict[str, Any],
    final_cues: int,
    unmapped: int,
    omitted: int,
    review: int,
    validation: ValidationResult | None,
    status: str,
    decisions: list[dict[str, Any]],
    cues: list[Cue] | None = None,
    merge_polish: dict[str, Any] | None = None,
) -> str:
    meta = raw.get("meta", {})
    diarization = meta.get("diarization", {})
    detected_speakers = len(diarization.get("detected_speakers", []))
    values = validation.to_dict() if validation else {}
    translation_history = _translation_history(decisions)
    return f"""# {config.code} 작업 결과 보고

- 생성: {APP_NAME} {APP_VERSION}
- 상태: {status}
- CUDA 사용 여부: {str(meta.get("device", "")).startswith("cuda")}
- 전사 장치: {meta.get("device", "unknown")}
- VAD 적용: {meta.get("vad_applied", False)} / {meta.get("vad", {})}
- 전사 모델/연산 형식: {meta.get("model", "unknown")} / {meta.get("compute_type", "unknown")}
- 마지막 번역 설정: {config.provider} / {config.model}
- 화자 분리: {"사용" if diarization.get("enabled") else "사용 안 함"}
- 화자 분리 모델/장치: {diarization.get("model", "-")} / {diarization.get("device", "-")}
- 화자 정렬/감지 화자 수: {diarization.get("alignment", "-")} / {detected_speakers}
- SRT 화자 라벨: {"표시" if config.diarization_srt_labels else "표시 안 함"}
{_translation_metrics_text(config)}- 원본 세그먼트 수: {len(raw.get("segments", []))}
- 최종 큐 수: {final_cues}
- 미매핑 수: {unmapped}
- 제외 수: {omitted}
- 검토 필요 수: {review}
- 겹침 수: {values.get("overlaps", 0)}
- 역전 수: {values.get("reversals", 0)}
- 타임코드 불일치 수: {values.get("timing_mismatches", 0)}
- 잘 자 오탐 수: {values.get("goodnight_false_positives", 0)}
- 언어 방향: {config.direction.label}
- 번역 자막에 남은 원문 문자 수: {values.get("japanese_residue", 0)}
- 금지 표식 수: {values.get("forbidden_markers", 0)}
- 번역 찌꺼기 후보 수: {values.get("junk_fragments", 0)}

## 구간별 번역 실행 기록

{translation_history}

## 병합 문장 경계 교정

{_merge_polish_report(merge_polish)}

## 구간별 대사 분포

{_coverage_text(raw, decisions, cues or [])}
"""


def _merge_polish_report(data: dict[str, Any] | None) -> str:
    records = list((data or {}).get("records") or [])
    if not records:
        return "- 교정 대상 없음"
    labels = {
        "accepted": "검증 통과·적용",
        "unchanged": "수정 불필요",
        "rejected": "검증 불통과·초안 유지",
        "held": "보류·초안 유지",
        "failed": "실행 실패·초안 유지",
    }
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status") or "failed")
        counts[status] = counts.get(status, 0) + 1
    lines = [
        "- "
        + ", ".join(
            f"{labels.get(status, status)} {count}개" for status, count in sorted(counts.items())
        )
    ]
    for record in records:
        raw_ids = [int(item) for item in record.get("raw_ids") or []]
        if not raw_ids:
            continue
        raw_range = str(raw_ids[0]) if len(raw_ids) == 1 else f"{raw_ids[0]}–{raw_ids[-1]}"
        profile = normalize_translation_profile(record.get("execution_profile"))
        profile_text = (
            f'{profile["provider"]} / {profile["model"]} · '
            f'{profile["reasoning_effort"]} / {profile["speed_mode"]}'
            if profile
            else "실행 프로필 미기록"
        )
        status = str(record.get("status") or "failed")
        lines.append(
            f'- raw_id {raw_range}: {labels.get(status, status)} · {profile_text} · '
            f'{record.get("processed_at") or "처리 시각 미기록"}'
        )
        if status == "accepted":
            lines.append(f'  - 초안: {record.get("initial_target", "")}')
            lines.append(f'  - 교정: {record.get("polished_target", "")}')
        elif record.get("reason"):
            lines.append(f'  - 사유: {record["reason"]}')
    return "\n".join(lines)


COVERAGE_BUCKET_SECONDS = 600


def _coverage_text(
    raw: dict[str, Any], decisions: list[dict[str, Any]], cues: list[Cue]
) -> str:
    """Lines per 10 minutes at each stage, to spot passages where dialogue went missing."""

    meta = raw.get("meta", {})
    segments = raw.get("segments", [])
    duration = float(meta.get("duration") or 0.0) or max(
        (float(item.get("end", 0.0)) for item in segments), default=0.0
    )
    buckets = max(1, int(duration // COVERAGE_BUCKET_SECONDS) + 1)
    raw_counts = [0] * buckets
    rescued = [0] * buckets
    translated = [0] * buckets
    cue_counts = [0] * buckets
    start_by_id = {int(item["id"]): float(item.get("start", 0.0)) for item in segments}

    def bucket(seconds: float) -> int:
        return min(buckets - 1, max(0, int(seconds // COVERAGE_BUCKET_SECONDS)))

    for item in segments:
        index = bucket(float(item.get("start", 0.0)))
        raw_counts[index] += 1
        if item.get("rescue"):
            rescued[index] += 1
    for decision in decisions:
        if decision.get("action") == "translated" and int(decision["raw_id"]) in start_by_id:
            translated[bucket(start_by_id[int(decision["raw_id"])])] += 1
    for cue in cues:
        cue_counts[bucket(cue.start)] += 1
    rescue = meta.get("rescue_pass") or {}
    lines = []
    if rescue:
        dropped = rescue.get("dropped_segments") or {}
        scope = f"30초 창 {rescue.get('windows', 0)}개"
        if rescue.get("intervals") is not None:
            scope += f" / 의심 공백 {rescue.get('intervals', 0)}개"
        lines.append(
            f"- 음성 감지 누락 구조: 다시 전사한 {scope}, "
            f"살린 줄 {rescue.get('kept_segments', 0)}개, 버린 줄 {sum(dropped.values())}개 "
            f"({', '.join(f'{key} {value}' for key, value in sorted(dropped.items())) or '-'})"
        )
    lines += ["", "| 구간 | 전사 줄 | 그중 구조 | 번역 줄 | 최종 자막 |", "|---|---|---|---|---|"]
    peak = max(raw_counts) if raw_counts else 0
    warnings = []
    for index in range(buckets):
        label = f"{index * 10}~{index * 10 + 10}분"
        lines.append(
            f"| {label} | {raw_counts[index]} | {rescued[index]} | {translated[index]} "
            f"| {cue_counts[index]} |"
        )
        last_partial = index == buckets - 1 and duration % COVERAGE_BUCKET_SECONDS < 300
        if peak >= 20 and raw_counts[index] * 10 < peak and not last_partial:
            warnings.append(label)
    if warnings:
        lines.append("")
        lines.append(
            "- 대사가 크게 줄어든 구간: " + ", ".join(warnings)
            + ". 실제로 대사가 적은 장면이 아니라면 전사 누락일 수 있습니다."
        )
    return "\n".join(lines)


def _translation_history(decisions: list[dict[str, Any]]) -> str:
    labels = {
        "batch": "일반 배치",
        "review_small": "검토 보강 배치",
        "review_single": "검토 보강 단건",
        "review_retry_user": "사용자 요청 단건 재번역",
        "manual": "직접 입력",
    }
    records: list[dict[str, Any]] = []
    for item in sorted(decisions, key=lambda value: int(value["raw_id"])):
        profile = normalize_translation_profile(item.get("execution_profile"))
        if profile is None:
            continue
        record = {
            "start": int(item["raw_id"]),
            "end": int(item["raw_id"]),
            "profile": profile,
            "kind": str(item.get("processing_kind", "batch")),
            "processed_at": str(item.get("processed_at", "")),
        }
        if records:
            previous = records[-1]
            same_execution = (
                previous["profile"] == record["profile"]
                and previous["kind"] == record["kind"]
                and previous["processed_at"] == record["processed_at"]
                and previous["end"] + 1 == record["start"]
            )
            if same_execution:
                previous["end"] = record["end"]
                continue
        records.append(record)
    if not records:
        return "- 기록 없음"
    lines: list[str] = []
    for record in records:
        profile = record["profile"]
        raw_range = (
            str(record["start"])
            if record["start"] == record["end"]
            else f'{record["start"]}–{record["end"]}'
        )
        lines.append(
            f'- raw_id {raw_range}: {labels.get(record["kind"], record["kind"])} · '
            f'{profile["provider"]} / {profile["model"]} · '
            f'{profile["reasoning_effort"]} / {profile["speed_mode"]} · '
            f'{record["processed_at"] or "처리 시각 미기록"}'
        )
    return "\n".join(lines)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _move_aside(paths: JobPaths, files: list[Path], reason: str) -> Path | None:
    """Move files into ``.subtitle_job/superseded/<timestamp>/`` instead of deleting them."""

    existing = [item for item in files if item.is_file()]
    if not existing:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target_dir = paths.superseded_dir / stamp
    target_dir.mkdir(parents=True, exist_ok=True)
    for item in existing:
        _replace_with_retry(item, target_dir / item.name)
    (target_dir / "README.txt").write_text(
        f"{reason}\n이동 시각: {stamp}\n", encoding="utf-8"
    )
    return target_dir


def retract_published_outputs(config: JobConfig, reason: str) -> Path | None:
    """Withdraw a previously published result set when the job is no longer complete.

    Only files this app published (sha256 recorded in the manifest) are moved; files
    that were changed or written by someone else stay where they are.
    """

    paths = JobPaths(config)
    manifest = JobManifest.load_or_create(config)
    published = manifest.stats.get("published") or {}
    recorded = published.get("files", {}) if isinstance(published, dict) else {}
    ours = [
        path
        for path in paths.final_outputs
        if path.is_file() and recorded.get(path.name) == _sha256(path)
    ]
    if not recorded:
        return None
    moved = _move_aside(
        paths, ours, f"작업이 완료 상태가 아니어서 이전 결과를 회수했습니다: {reason}"
    )
    manifest.stats["published"] = None
    manifest.save()
    return moved


def publish_outputs(config: JobConfig) -> list[Path]:
    """Publish the six staged files to the output folder as one set.

    Every file is first copied next to its target under a temporary name; only after
    all copies succeed are they renamed into place. Files in the way that this app did
    not publish (for example a raw JSON from an older pipeline) are moved aside first.
    """

    paths = JobPaths(config)
    sources = list(paths.staged_outputs)
    targets = list(paths.final_outputs)
    missing = [source for source in sources if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"내보낼 결과 파일이 없습니다: {missing[0]}")
    manifest = JobManifest.load_or_create(config)
    published = manifest.stats.get("published") or {}
    recorded = published.get("files", {}) if isinstance(published, dict) else {}
    foreign = [
        target
        for target in targets
        if target.is_file() and recorded.get(target.name) != _sha256(target)
    ]
    _move_aside(paths, foreign, "새 결과를 내보내기 전에 기존 파일을 보관했습니다.")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    temporaries: list[tuple[Path, Path]] = []
    replaced: list[Path] = []
    try:
        for source, target in zip(sources, targets, strict=True):
            descriptor, name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(name)
            temporaries.append((temporary, target))
            shutil.copy2(source, temporary)
        for temporary, target in temporaries:
            _replace_with_retry(temporary, target)
            replaced.append(target)
    except BaseException:
        for temporary, _target in temporaries:
            temporary.unlink(missing_ok=True)
        if replaced:
            # Record what did land so a later retract recognises these files as ours.
            manifest.stats["published"] = {
                "at": datetime.now(UTC).isoformat(),
                "partial": True,
                "files": {target.name: _sha256(target) for target in replaced},
            }
            manifest.save()
        raise
    manifest.stats["published"] = {
        "at": datetime.now(UTC).isoformat(),
        "files": {target.name: _sha256(target) for target in targets},
    }
    manifest.save()
    return targets


def build_and_validate(config: JobConfig) -> ValidationResult:
    paths = JobPaths(config)
    manifest = JobManifest.load_or_create(config)
    if not paths.raw.exists() or not paths.decisions.exists():
        raise FileNotFoundError("원본 전사 또는 번역 결정 파일이 없습니다.")
    raw = load_json(paths.raw)
    segments = list(raw.get("segments", []))
    decision_data = load_json(paths.decisions)
    try:
        merge_polish = (
            load_json(paths.merged_cue_polish) if paths.merged_cue_polish.is_file() else {}
        )
    except (OSError, ValueError, TypeError):
        merge_polish = {}
    polish_records = {
        tuple(int(raw_id) for raw_id in record.get("raw_ids") or []): record
        for record in merge_polish.get("records") or []
        if isinstance(record, dict) and record.get("raw_ids")
    }
    if decision_data.get("partial") or manifest.status == JobStatus.TRANSLATION_PARTIAL:
        refused_ids = [int(item) for item in decision_data.get("refused_raw_ids", [])]
        message = (
            "거절된 번역 배치 또는 보류 항목이 남아 있어 최종 SRT 생성을 시작할 수 없습니다. "
            f"영향받은 raw_id는 {refused_ids}입니다."
        )
        manifest.set_status(JobStatus.TRANSLATION_PARTIAL, error=message)
        retract_published_outputs(config, message)
        raise RuntimeError(message)
    decisions = list(decision_data.get("items", []))
    segment_ids = {int(item.get("id", index)) for index, item in enumerate(segments)}
    decision_ids = [int(item["raw_id"]) for item in decisions]
    unmapped_ids = sorted(segment_ids - set(decision_ids))
    duplicate_ids = sorted({item for item in decision_ids if decision_ids.count(item) > 1})
    unmapped = len(unmapped_ids) + len(duplicate_ids)
    review_items = [item for item in decisions if item["action"] == "review"]
    omitted_items = [item for item in decisions if item["action"] == "omitted"]
    segment_by_id = {int(item.get("id", index)): item for index, item in enumerate(segments)}
    staged_review = paths.staged(paths.review_tsv)
    staged_omitted = paths.staged(paths.omitted_tsv)
    staged_report = paths.staged(paths.report)
    staged_ja = paths.staged(paths.ja_srt)
    staged_ko = paths.staged(paths.ko_srt)
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    for stale in (staged_ja, staged_ko):
        stale.unlink(missing_ok=True)

    _write_tsv(
        staged_review,
        ["raw_id", "start", "end", "raw_ja", "ja_fixed", "ko", "reason", "register_note"],
        [
            [
                item["raw_id"],
                segment_by_id[int(item["raw_id"])]["start"],
                segment_by_id[int(item["raw_id"])]["end"],
                segment_by_id[int(item["raw_id"])].get("text", ""),
                item.get("ja_fixed", ""),
                item.get("ko", ""),
                item.get("reason", ""),
                item.get("register_note", ""),
            ]
            for item in review_items
        ],
    )
    _write_tsv(
        staged_omitted,
        ["raw_id", "start", "end", "raw_ja", "reason", "source"],
        [
            [
                item["raw_id"],
                segment_by_id[int(item["raw_id"])]["start"],
                segment_by_id[int(item["raw_id"])]["end"],
                segment_by_id[int(item["raw_id"])].get("text", ""),
                item.get("reason", ""),
                item.get("source", ""),
            ]
            for item in omitted_items
        ],
    )

    if unmapped or review_items:
        status = "검토 필요" if review_items else "미매핑으로 중단"
        atomic_write_text(
            staged_report,
            _report_text(
                config=config,
                raw=raw,
                final_cues=0,
                unmapped=unmapped,
                omitted=len(omitted_items),
                review=len(review_items),
                validation=None,
                status=status,
                decisions=decisions,
                merge_polish=merge_polish,
            ),
        )
        manifest.stats.update({"unmapped_segments": unmapped, "review_segments": len(review_items)})
        manifest.set_status(JobStatus.PARTIAL_REVIEW if review_items else JobStatus.FAILED)
        message = (
            f"최종 자막을 만들 수 없습니다. 미매핑 {unmapped}개, "
            f"검토 필요 {len(review_items)}개입니다."
        )
        retract_published_outputs(config, message)
        raise RuntimeError(message)

    manifest.stats["current_stage"] = "output"
    manifest.set_status(JobStatus.BUILDING)
    cues = _build_cues(
        segments,
        decisions,
        respect_speakers=config.diarization_enabled,
        language_pair=config.language_pair,
        polishes=polish_records,
    )
    atomic_write_text(
        staged_ja,
        _render_srt(cues, "ja", show_speakers=config.diarization_srt_labels),
        encoding="utf-8-sig",
    )
    atomic_write_text(
        staged_ko,
        _render_srt(cues, "ko", show_speakers=config.diarization_srt_labels),
        encoding="utf-8-sig",
    )
    manifest.set_status(JobStatus.VALIDATING)
    duration = float(raw.get("meta", {}).get("duration", 0.0)) or None
    validation = validate_srt_pair(
        staged_ja, staged_ko, duration=duration, language_pair=config.language_pair
    )
    atomic_write_text(
        staged_report,
        _report_text(
            config=config,
            raw=raw,
            final_cues=len(cues),
            unmapped=0,
            omitted=len(omitted_items),
            review=0,
            validation=validation,
            status="완료" if validation.ok else "검증 실패",
            decisions=decisions,
            cues=cues,
            merge_polish=merge_polish,
        ),
    )
    manifest.stats.update(
        {
            "final_cues": len(cues),
            "unmapped_segments": 0,
            "omitted_segments": len(omitted_items),
            "validation": validation.to_dict(),
        }
    )
    if not validation.ok:
        manifest.stats.pop("current_stage", None)
        manifest.set_status(JobStatus.FAILED)
        message = f"SRT 검증에 실패했습니다: {validation.to_dict()}"
        retract_published_outputs(config, message)
        raise RuntimeError(message)
    manifest.save()
    publish_outputs(config)
    manifest = JobManifest.load_or_create(config)
    manifest.stats.pop("current_stage", None)
    manifest.set_status(JobStatus.COMPLETED)
    return validation


def export_outputs(config: JobConfig, destination: Path) -> list[Path]:
    """Copy the published six-file set to another folder (atomic per file)."""

    paths = JobPaths(config)
    sources = list(paths.final_outputs)
    missing = [source for source in sources if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"내보낼 결과 파일이 없습니다: {missing[0]}")
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []
    for source in sources:
        target = destination / source.name
        if source.resolve() == target.resolve():
            exported.append(target)
            continue
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=destination,
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(source, temporary)
            _replace_with_retry(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        exported.append(target)
    return exported
