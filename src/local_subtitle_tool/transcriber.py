from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .core import (
    Accelerator,
    ComputeType,
    JobConfig,
    JobManifest,
    JobPaths,
    JobStatus,
    atomic_write_json,
    load_json,
)
from .diarization import (
    DiarizationCancelled,
    DiarizationError,
    apply_diarization,
)
from .model_manager import inspect_model_cache
from .policy import (
    _NORMALIZED_GENERIC_PHRASES,
    DEFAULT_VAD_PARAMETERS,
    POLICY_SOURCE,
    POLICY_VERSION,
    _normalize,
    decode_options,
)
from .preflight import check_cpu_runtime, check_cuda, probe_media
from .repetition import collapse_repeats
from .subtitles import retract_published_outputs

ProgressCallback = Callable[[str], None]
ProgressValueCallback = Callable[[float | None, str], None]

MODEL_NAME = "large-v3"
# 2: Silero VAD is applied per 30-second window (schema 1 checkpoints were decoded
#    with clip_timestamps, where faster-whisper silently ignores vad_filter).
# 3: rescue pass (windows where VAD heard almost nothing but the audio is loud are
#    decoded whole) and collapsed phrase loops.
# 4: also rescue loud internal gaps, so quiet speech is not hidden by louder speech in
#    the same 30-second window.
CHECKPOINT_SCHEMA_VERSION = 4
LEGACY_CHECKPOINT_SCHEMA_VERSION = 3

# Rescue pass. Silero VAD can classify short lines buried in moans and background
# sound as non-speech (테스트 영상 E: 1 s of "speech" in 70-107 min while the audio was
# loud; decoding without VAD found real lines). Such windows are decoded whole and
# the result is filtered harder than ordinary VAD output.
RESCUE_MAX_VAD_SECONDS = 3.0
RESCUE_MIN_DBFS = -45.0
RESCUE_MAX_NO_SPEECH = 0.6
RESCUE_MIN_LOGPROB = -1.0
RESCUE_MAX_COMPRESSION = 2.4
RESCUE_MAX_CHARACTER_RUN = 15
RESCUE_OVERLAP_RATIO = 0.4
RESCUE_DUPLICATE_SECONDS = 3.0
RESCUE_DUPLICATE_TEXT_RATIO = 0.65
RESCUE_MIN_GAP_SECONDS = 2.5
RESCUE_GAP_PAD_SECONDS = 0.5
# Keep checkpoint writes and cancellation opportunities reasonably frequent while the
# direct Whisper rescue pass examines suspect windows. BatchedInferencePipeline is not
# used here: it skipped a short real line that the ordinary decoder recovered.
RESCUE_BATCH_WINDOWS = 16
VAD_SCHEMA_VERSION = 1
CLIP_SECONDS = 30.0
SAMPLING_RATE = 16000


class TranscriptionCancelled(RuntimeError):
    pass


class CPUFallbackConfirmationRequired(RuntimeError):
    pass


class ModelDownloadRequired(RuntimeError):
    pass


class TranscriptionDeviceError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class ClipWindow:
    index: int
    start: float
    end: float


@dataclass(slots=True, frozen=True)
class TranscriptionRuntime:
    accelerator: str
    device: str
    device_index: int | None
    compute_type: str
    cpu_threads: int


def _log(callback: ProgressCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _clip_windows(duration_seconds: float) -> list[ClipWindow]:
    windows: list[ClipWindow] = []
    start = 0.0
    index = 0
    while start < duration_seconds:
        windows.append(
            ClipWindow(index=index, start=start, end=min(start + CLIP_SECONDS, duration_seconds))
        )
        start += CLIP_SECONDS
        index += 1
    return windows


def _checkpoint_path(paths: JobPaths, clip_index: int) -> Path:
    return paths.clips_dir / f"{clip_index:06d}.json"


def _source_fingerprint(config: JobConfig, duration: float) -> str:
    stat = config.video_path.stat()
    payload = {
        "path": str(config.video_path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration": round(duration, 3),
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _accepted_raw_fingerprints(language: str = "ja") -> set[str]:
    """Finished raw transcriptions reusable without missing the new rescue pass."""

    return {_settings_fingerprint(language=language)}


def _absolute_segment(segment: Any, duration: float) -> dict[str, Any]:
    """A batched-pipeline segment (already in absolute time) as a raw segment dict."""

    def clamp(value: float) -> float:
        return round(min(duration, max(0.0, float(value))), 3)

    return {
        "seek": int(getattr(segment, "seek", 0) or 0),
        "start": clamp(segment.start),
        "end": clamp(segment.end),
        "text": collapse_repeats(segment.text.strip()),
        "tokens": list(segment.tokens or []),
        "avg_logprob": float(segment.avg_logprob),
        "compression_ratio": float(segment.compression_ratio),
        "no_speech_prob": float(segment.no_speech_prob),
        "words": [
            {
                "start": clamp(word.start),
                "end": clamp(word.end),
                "word": str(word.word),
                "probability": float(word.probability),
            }
            for word in segment.words or []
            if word.start is not None and word.end is not None
        ],
    }


_CURRENT_RESCUE_DECODE = object()


def _settings_fingerprint(
    *,
    legacy: bool = False,
    rescue_decode: object = _CURRENT_RESCUE_DECODE,
    language: str = "ja",
    hotwords: str = "",
) -> str:
    """Transcription policy fingerprint; ``legacy`` gives the 0.8.7 policy (no rescue)."""

    payload: dict[str, Any] = {
        "model": MODEL_NAME,
        "language": language,
        "decode": decode_options(),
        "vad": dict(DEFAULT_VAD_PARAMETERS),
        "vad_mode": "silero-whole-file-split-per-window",
        "decode_unit": "contiguous-speech-groups",
        "window_assignment": "chunk-start-window-v1",
        "speech_group_gap_seconds": SPEECH_GROUP_GAP_SECONDS,
        "clip_seconds": CLIP_SECONDS,
        "checkpoint_schema": (
            LEGACY_CHECKPOINT_SCHEMA_VERSION if legacy else CHECKPOINT_SCHEMA_VERSION
        ),
        "policy_version": POLICY_VERSION,
    }
    if not legacy:
        payload["rescue"] = {
            "max_vad_seconds": RESCUE_MAX_VAD_SECONDS,
            "min_dbfs": RESCUE_MIN_DBFS,
            "max_no_speech": RESCUE_MAX_NO_SPEECH,
            "min_logprob": RESCUE_MIN_LOGPROB,
            "max_compression": RESCUE_MAX_COMPRESSION,
            "max_character_run": RESCUE_MAX_CHARACTER_RUN,
            "overlap_ratio": RESCUE_OVERLAP_RATIO,
            "duplicate_seconds": RESCUE_DUPLICATE_SECONDS,
            "duplicate_text_ratio": RESCUE_DUPLICATE_TEXT_RATIO,
            "min_gap_seconds": RESCUE_MIN_GAP_SECONDS,
            "gap_pad_seconds": RESCUE_GAP_PAD_SECONDS,
            "gap_coverage": "first-pass-lines-cross-window-v2",
        }
        payload["repeat_collapse"] = "phrase-run-4-to-3-v1"
        if hotwords:
            payload["hotwords"] = hotwords
        if rescue_decode is _CURRENT_RESCUE_DECODE:
            payload["rescue_decode"] = "direct-gap-triggered-window-context-no-hotwords-v6"
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _window_dbfs(audio, window: ClipWindow) -> float:
    import numpy as np

    start = round(window.start * SAMPLING_RATE)
    end = round(window.end * SAMPLING_RATE)
    clip = audio[start:end]
    if len(clip) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(clip.astype("float32", copy=False)))))
    return 20.0 * float(np.log10(max(rms, 1e-6)))


def _needs_rescue(speech_seconds: float, dbfs: float) -> bool:
    return speech_seconds < RESCUE_MAX_VAD_SECONDS and dbfs >= RESCUE_MIN_DBFS


def _rescue_spans(
    audio, window: ClipWindow, segments: list[dict[str, Any]]
) -> list[ClipWindow]:
    """Return audible, uncovered spans that deserve a no-VAD second pass.

    Looking only at total VAD speech misses the important case where loud dialogue in
    one part of a 30-second window hides quiet dialogue elsewhere in that same window.
    Use the first-pass segment coverage instead, and decode only sufficiently long,
    non-silent gaps with a little surrounding context.
    """

    covered: list[tuple[float, float]] = []
    for segment in segments:
        start = max(window.start, min(window.end, float(segment.get("start", 0.0))))
        end = max(window.start, min(window.end, float(segment.get("end", 0.0))))
        if end <= start:
            continue
        if covered and start <= covered[-1][1]:
            covered[-1] = (covered[-1][0], max(covered[-1][1], end))
        else:
            covered.append((start, end))

    gaps: list[tuple[float, float]] = []
    cursor = window.start
    for start, end in covered:
        if start - cursor >= RESCUE_MIN_GAP_SECONDS:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if window.end - cursor >= RESCUE_MIN_GAP_SECONDS:
        gaps.append((cursor, window.end))

    spans: list[ClipWindow] = []
    for start, end in gaps:
        probe = ClipWindow(index=window.index, start=start, end=end)
        if _window_dbfs(audio, probe) < RESCUE_MIN_DBFS:
            continue
        padded = ClipWindow(
            index=window.index,
            start=max(window.start, start - RESCUE_GAP_PAD_SECONDS),
            end=min(window.end, end + RESCUE_GAP_PAD_SECONDS),
        )
        if spans and padded.start <= spans[-1].end:
            previous = spans[-1]
            spans[-1] = ClipWindow(
                index=window.index,
                start=previous.start,
                end=max(previous.end, padded.end),
            )
        else:
            spans.append(padded)
    return spans


def _is_term_echo(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    """True when a line is nothing but the user's term hints (hallucinated from hotwords)."""

    import re

    if not terms:
        return False
    rest = text
    for term in sorted(terms, key=len, reverse=True):
        rest = rest.replace(term, "")
    return not re.sub(r"[\W_]+", "", rest)


def _rescue_reject_reason(
    segment: dict[str, Any],
    kept: list[dict[str, Any]],
    terms: tuple[str, ...] | list[str] = (),
) -> str | None:
    """Why a whole-window (no VAD) segment is dropped, or None to keep it."""

    import re

    text = str(segment.get("text", "")).strip()
    if not text:
        return "empty"
    if _is_term_echo(text, terms):
        return "term_echo"
    if float(segment.get("no_speech_prob", 0.0)) > RESCUE_MAX_NO_SPEECH:
        return "no_speech"
    if float(segment.get("avg_logprob", 0.0)) < RESCUE_MIN_LOGPROB:
        return "low_confidence"
    if float(segment.get("compression_ratio", 0.0)) > RESCUE_MAX_COMPRESSION or re.search(
        rf"(.)\1{{{RESCUE_MAX_CHARACTER_RUN - 1},}}", text
    ):
        return "repetition"
    if _normalize(text) in _NORMALIZED_GENERIC_PHRASES:
        return "generic_phrase"
    start, end = float(segment["start"]), float(segment["end"])
    length = max(end - start, 0.001)
    # Overlap with all kept lines together: one long rescued line can repeat two
    # short first-pass lines that each cover less than the threshold.
    covered = 0.0
    cursor = start
    for other in sorted(kept, key=lambda item: float(item["start"])):
        other_start = max(cursor, float(other["start"]))
        other_end = min(end, float(other["end"]))
        if other_end > other_start:
            covered += other_end - other_start
            cursor = other_end
    if covered / length >= RESCUE_OVERLAP_RATIO:
        return "overlap"
    normalized = _normalize(text)
    for other in kept:
        other_text = _normalize(str(other.get("text", "")))
        if not other_text or abs(float(other["start"]) - start) > RESCUE_DUPLICATE_SECONDS:
            continue
        # The same line heard twice, possibly with a word more or less
        # ("어디 가셨지?" / "쌤이 어디 가셨지?").
        shorter = min(len(normalized), len(other_text))
        longer = max(len(normalized), len(other_text))
        if normalized == other_text or (
            shorter >= 4
            and shorter / longer >= RESCUE_DUPLICATE_TEXT_RATIO
            and (normalized in other_text or other_text in normalized)
        ):
            return "duplicate"
    return None


def _recommended_cpu_threads() -> int:
    return max(1, (os.cpu_count() or 1) - 1)


def _resolve_runtime(config: JobConfig) -> TranscriptionRuntime:
    if config.accelerator in {Accelerator.AUTO.value, Accelerator.CUDA.value}:
        cuda = check_cuda()
        if cuda.ok:
            count = int(cuda.details.get("count", 0))
            if config.device_index >= count:
                raise TranscriptionDeviceError(
                    f"CUDA 장치 {config.device_index}번을 사용할 수 없습니다. 감지 장치 수={count}"
                )
            if config.compute_type not in {ComputeType.AUTO.value, ComputeType.FLOAT16.value}:
                raise ValueError("CUDA 전사는 float16 연산만 지원합니다.")
            return TranscriptionRuntime(
                accelerator=Accelerator.CUDA.value,
                device=Accelerator.CUDA.value,
                device_index=config.device_index,
                compute_type=ComputeType.FLOAT16.value,
                cpu_threads=config.cpu_threads or 8,
            )
        if config.accelerator == Accelerator.CUDA.value:
            raise TranscriptionDeviceError(cuda.message)
        cpu = check_cpu_runtime()
        if cpu.ok:
            raise CPUFallbackConfirmationRequired(
                "CUDA를 사용할 수 없습니다. CPU int8 전사를 사용하려면 연산 장치를 CPU로 "
                "명시적으로 선택하십시오."
            )
        raise TranscriptionDeviceError(f"{cuda.message} {cpu.message}")

    cpu = check_cpu_runtime()
    if not cpu.ok:
        raise TranscriptionDeviceError(cpu.message)
    if config.compute_type not in {ComputeType.AUTO.value, ComputeType.INT8.value}:
        raise ValueError("CPU 전사는 int8 연산만 지원합니다.")
    return TranscriptionRuntime(
        accelerator=Accelerator.CPU.value,
        device=Accelerator.CPU.value,
        device_index=None,
        compute_type=ComputeType.INT8.value,
        cpu_threads=config.cpu_threads or _recommended_cpu_threads(),
    )


# --------------------------------------------------------------------------------------
# Audio and VAD
# --------------------------------------------------------------------------------------


def _decode_audio(video_path: Path):
    _patch_av_open()
    from faster_whisper.audio import decode_audio

    return decode_audio(str(video_path), sampling_rate=SAMPLING_RATE)


def _detect_speech(audio) -> list[dict[str, int]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(**DEFAULT_VAD_PARAMETERS)
    return [
        {"start": int(item["start"]), "end": int(item["end"])}
        for item in get_speech_timestamps(audio, options, sampling_rate=SAMPLING_RATE)
    ]


def _load_or_detect_speech(
    paths: JobPaths,
    audio,
    *,
    source_fingerprint: str,
    settings_fingerprint: str,
) -> list[dict[str, int]]:
    if paths.vad.is_file():
        try:
            cached = load_json(paths.vad)
            if (
                cached.get("schema_version") == VAD_SCHEMA_VERSION
                and cached.get("source_fingerprint") == source_fingerprint
                and cached.get("settings_fingerprint") == settings_fingerprint
                and isinstance(cached.get("chunks"), list)
            ):
                return [
                    {"start": int(item["start"]), "end": int(item["end"])}
                    for item in cached["chunks"]
                ]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
    chunks = _detect_speech(audio)
    atomic_write_json(
        paths.vad,
        {
            "schema_version": VAD_SCHEMA_VERSION,
            "source_fingerprint": source_fingerprint,
            "settings_fingerprint": settings_fingerprint,
            "sampling_rate": SAMPLING_RATE,
            "vad": dict(DEFAULT_VAD_PARAMETERS),
            "chunks": chunks,
        },
    )
    return chunks


def _window_speech_chunks(
    chunks: list[dict[str, int]], window: ClipWindow
) -> list[dict[str, int]]:
    """Speech chunks that start in this window, relative to the window start.

    A chunk belongs to the window it starts in and is kept whole, even when it runs
    past the 30-second boundary, so an utterance is never cut in two decodes. The
    next window does not see it again.
    """

    window_start = round(window.start * SAMPLING_RATE)
    window_end = round(window.end * SAMPLING_RATE)
    return [
        {"start": chunk["start"] - window_start, "end": chunk["end"] - window_start}
        for chunk in chunks
        if window_start <= chunk["start"] < window_end and chunk["end"] > chunk["start"]
    ]


def _window_speech_coverage(
    chunks: list[dict[str, int]], window: ClipWindow
) -> list[dict[str, float]]:
    """Absolute VAD coverage intersecting a window, including boundary crossings."""

    window_start = round(window.start * SAMPLING_RATE)
    window_end = round(window.end * SAMPLING_RATE)
    coverage: list[dict[str, float]] = []
    for chunk in chunks:
        start = max(window_start, int(chunk["start"]))
        end = min(window_end, int(chunk["end"]))
        if end <= start:
            continue
        coverage.append(
            {
                "start": start / SAMPLING_RATE,
                "end": end / SAMPLING_RATE,
            }
        )
    return coverage


SPEECH_GROUP_GAP_SECONDS = 2.0


def _speech_groups(local_chunks: list[dict[str, int]]) -> list[dict[str, int]]:
    """Merge VAD chunks separated by at most ``SPEECH_GROUP_GAP_SECONDS`` of silence.

    Each group is decoded as one continuous span of the original audio. Splicing
    speech chunks together (what faster-whisper's own ``vad_filter`` does) leaves
    Whisper's word timestamps ambiguous at every splice, which can move a word by many
    seconds; decoding contiguous spans keeps timestamps exact. Silences longer than the
    gap never reach Whisper.
    """

    gap = round(SPEECH_GROUP_GAP_SECONDS * SAMPLING_RATE)
    groups: list[dict[str, int]] = []
    for chunk in local_chunks:
        if groups and chunk["start"] - groups[-1]["end"] <= gap:
            groups[-1]["end"] = max(groups[-1]["end"], chunk["end"])
        else:
            groups.append({"start": chunk["start"], "end": chunk["end"]})
    return groups


def _group_audio(audio, window: ClipWindow, group: dict[str, int]):
    window_start = round(window.start * SAMPLING_RATE)
    return audio[window_start + group["start"] : window_start + group["end"]].astype(
        "float32", copy=False
    )


def _restore_segments(segments, group: dict[str, int]) -> list[dict[str, Any]]:
    """Window-relative segment dicts for one decoded group (exact offset, clamped)."""

    group_start = group["start"] / SAMPLING_RATE
    group_end = group["end"] / SAMPLING_RATE

    def shift(value: float) -> float:
        return min(group_end, max(group_start, group_start + float(value)))

    restored: list[dict[str, Any]] = []
    for segment in segments:
        words = [
            {
                "start": shift(word.start),
                "end": shift(word.end),
                "word": str(word.word),
                "probability": float(word.probability),
            }
            for word in segment.words or []
            if word.start is not None and word.end is not None
        ]
        restored.append(
            {
                "seek": int(segment.seek),
                "start": shift(segment.start),
                "end": shift(segment.end),
                "text": collapse_repeats(segment.text.strip()),
                "tokens": list(segment.tokens),
                "avg_logprob": float(segment.avg_logprob),
                "compression_ratio": float(segment.compression_ratio),
                "no_speech_prob": float(segment.no_speech_prob),
                "words": words,
            }
        )
    return restored


# --------------------------------------------------------------------------------------
# Checkpoints
# --------------------------------------------------------------------------------------


def _serialize_segment(
    segment: dict[str, Any], window: ClipWindow, duration: float
) -> dict[str, object]:
    """Convert a restored (window-relative) segment to absolute times within the media."""

    def absolute(value: float) -> float:
        return round(min(duration, max(window.start, window.start + float(value))), 3)

    serialized = dict(segment)
    serialized["start"] = absolute(segment["start"])
    serialized["end"] = absolute(segment["end"])
    serialized["words"] = [
        {**word, "start": absolute(word["start"]), "end": absolute(word["end"])}
        for word in segment.get("words", [])
    ]
    return serialized


def _checkpoint_payload_valid(
    payload: object,
    window: ClipWindow,
    *,
    source_fingerprint: str,
    settings_fingerprint: str,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        return False
    if payload.get("source_fingerprint") != source_fingerprint:
        return False
    if payload.get("settings_fingerprint") != settings_fingerprint:
        return False
    try:
        clip_index = int(payload.get("clip_index", -1))
    except (TypeError, ValueError):
        return False
    if clip_index != window.index:
        return False
    try:
        start = float(payload["start"])
        end = float(payload["end"])
    except (KeyError, TypeError, ValueError):
        return False
    if abs(start - window.start) > 0.001 or abs(end - window.end) > 0.001:
        return False
    return isinstance(payload.get("segments"), list)


def _load_checkpoints(
    paths: JobPaths,
    windows: list[ClipWindow],
    *,
    source_fingerprint: str,
    settings_fingerprint: str,
    progress: ProgressCallback | None,
) -> dict[int, dict[str, Any]]:
    valid: dict[int, dict[str, Any]] = {}
    for window in windows:
        path = _checkpoint_path(paths, window.index)
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _log(progress, f"클립 {window.index + 1} 체크포인트가 손상되어 다시 처리합니다.")
            continue
        if _checkpoint_payload_valid(
            payload,
            window,
            source_fingerprint=source_fingerprint,
            settings_fingerprint=settings_fingerprint,
        ):
            valid[window.index] = payload
        else:
            _log(progress, f"클립 {window.index + 1} 체크포인트 설정이 달라 다시 처리합니다.")
    return valid


def _save_checkpoint(
    paths: JobPaths,
    window: ClipWindow,
    segments: list[dict[str, object]],
    *,
    source_fingerprint: str,
    settings_fingerprint: str,
    runtime: TranscriptionRuntime | None,
    speech_seconds: float,
    language: str,
    language_probability: float,
    rescue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "clip_index": window.index,
        "start": window.start,
        "end": window.end,
        "source_fingerprint": source_fingerprint,
        "settings_fingerprint": settings_fingerprint,
        "runtime": asdict(runtime) if runtime else {},
        "speech_seconds": round(speech_seconds, 3),
        "language": language,
        "language_probability": language_probability,
        "completed_at": datetime.now(UTC).isoformat(),
        "segments": segments,
    }
    if rescue is not None:
        payload["rescue"] = rescue
    atomic_write_json(_checkpoint_path(paths, window.index), payload)
    return payload


def _merge_checkpoints(
    config: JobConfig,
    paths: JobPaths,
    windows: list[ClipWindow],
    checkpoints: dict[int, dict[str, Any]],
    *,
    duration: float,
    source_fingerprint: str,
    settings_fingerprint: str,
) -> list[dict[str, object]]:
    missing = [window.index for window in windows if window.index not in checkpoints]
    if missing:
        raise RuntimeError(f"병합할 수 없는 미완료 전사 클립이 있습니다: {missing}")

    merged: list[dict[str, object]] = []
    runtimes: list[dict[str, Any]] = []
    language = config.direction.source
    language_probability = 1.0
    speech_seconds = 0.0
    rescue_windows = 0
    rescue_intervals = 0
    rescue_kept = 0
    rescue_dropped: dict[str, int] = {}
    for window in windows:
        payload = checkpoints[window.index]
        rescue = payload.get("rescue") or {}
        if rescue.get("decoded"):
            rescue_windows += 1
            rescue_intervals += len(rescue.get("intervals") or []) or 1
            rescue_kept += int(rescue.get("kept", 0))
            for reason, count in (rescue.get("dropped") or {}).items():
                rescue_dropped[reason] = rescue_dropped.get(reason, 0) + int(count)
        runtime = dict(payload.get("runtime", {}))
        if runtime and runtime not in runtimes:
            runtimes.append(runtime)
        language = str(payload.get("language", language))
        language_probability = float(payload.get("language_probability", language_probability))
        speech_seconds += float(payload.get("speech_seconds", 0.0))
        for item in payload["segments"]:
            segment = dict(item)
            segment["clip_index"] = window.index
            merged.append(segment)
    merged.sort(
        key=lambda item: (
            float(item["start"]),
            float(item["end"]),
            int(item["clip_index"]),
        )
    )
    output_segments: list[dict[str, object]] = []
    for raw_id, item in enumerate(merged):
        segment = dict(item)
        segment.pop("clip_index", None)
        segment["id"] = raw_id
        output_segments.append(segment)

    runtime_devices = sorted({str(item.get("device", "unknown")) for item in runtimes})
    runtime_compute_types = sorted(
        {str(item.get("compute_type", "unknown")) for item in runtimes}
    )
    if len(runtimes) == 1 and runtimes[0].get("device") == Accelerator.CUDA.value:
        device_label = f"cuda:{int(runtimes[0].get('device_index', 0))}"
    elif len(runtime_devices) == 1:
        device_label = runtime_devices[0]
    elif not runtimes:
        device_label = "none"
    else:
        device_label = "mixed"
    payload = {
        "meta": {
            "code": config.code,
            "source": str(config.video_path),
            "source_fingerprint": source_fingerprint,
            "settings_fingerprint": settings_fingerprint,
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "completed_clips": len(windows),
            "model": f"faster-whisper {MODEL_NAME}",
            "device": device_label,
            "compute_type": (
                runtime_compute_types[0] if len(runtime_compute_types) == 1 else "mixed"
            ),
            "runtimes": runtimes,
            "language": language,
            "language_probability": language_probability,
            "duration": duration,
            "speech_seconds": round(speech_seconds, 3),
            "beam_size": 5,
            "temperature": 0.0,
            "condition_on_previous_text": False,
            "vad": dict(DEFAULT_VAD_PARAMETERS),
            "vad_applied": True,
            "vad_mode": "silero-whole-file-split-per-window",
            "clip_seconds": CLIP_SECONDS,
            "word_timestamps": True,
            "policy_source": POLICY_SOURCE,
            "rescue_pass": {
                "windows": rescue_windows,
                "intervals": rescue_intervals,
                "kept_segments": rescue_kept,
                "dropped_segments": rescue_dropped,
                "max_vad_seconds": RESCUE_MAX_VAD_SECONDS,
                "min_dbfs": RESCUE_MIN_DBFS,
                "min_gap_seconds": RESCUE_MIN_GAP_SECONDS,
                "gap_pad_seconds": RESCUE_GAP_PAD_SECONDS,
            },
        },
        "segments": output_segments,
    }
    atomic_write_json(paths.raw, payload)
    return output_segments


def _raw_matches(path: Path, *, source_fingerprint: str, settings_fingerprint: str) -> bool:
    try:
        meta = load_json(path).get("meta", {})
    except (OSError, ValueError, AttributeError, json.JSONDecodeError):
        return False
    return (
        meta.get("source_fingerprint") == source_fingerprint
        and meta.get("settings_fingerprint") == settings_fingerprint
    )


def _patch_av_open() -> None:
    import av

    if getattr(av.open, "_local_subtitle_compatible", False):
        return
    original_av_open = av.open

    def compatible_av_open(*args: object, **kwargs: object):
        kwargs.pop("metadata_errors", None)
        return original_av_open(*args, **kwargs)

    compatible_av_open._local_subtitle_compatible = True  # type: ignore[attr-defined]
    av.open = compatible_av_open  # type: ignore[assignment]


def transcribe(
    config: JobConfig,
    *,
    overwrite: bool = False,
    progress: ProgressCallback | None = None,
    progress_value: ProgressValueCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    paths = JobPaths(config)
    manifest = JobManifest.load_or_create(config)
    active_mark: float | None = None
    transcription_share = 0.88 if config.diarization_enabled else 1.0

    def record_active_time() -> None:
        nonlocal active_mark
        if active_mark is None:
            return
        now = time.monotonic()
        manifest.active_elapsed_seconds += max(0.0, now - active_mark)
        active_mark = now

    def check_cancel() -> None:
        if cancel_event and cancel_event.is_set():
            raise TranscriptionCancelled("사용자 요청으로 전사를 중지했습니다.")

    def apply_speaker_context() -> None:
        nonlocal active_mark
        if config.diarization_enabled and active_mark is None:
            active_mark = time.monotonic()
        apply_diarization(
            config,
            progress=progress,
            progress_value=(
                (
                    lambda fraction, message: progress_value(
                        0.88 + 0.12 * max(0.0, min(1.0, fraction)),
                        message,
                    )
                )
                if progress_value and config.diarization_enabled
                else None
            ),
            cancel_event=cancel_event,
        )
        if config.diarization_enabled:
            record_active_time()

    def finish(output_count: int | None, duration: float, clip_count: int, message: str) -> Path:
        apply_speaker_context()
        if output_count is not None:
            manifest.stats["raw_segments"] = output_count
        manifest.stats["duration"] = duration
        manifest.stats["completed_clips"] = clip_count
        record_active_time()
        manifest.stats.pop("current_stage", None)
        manifest.set_status(JobStatus.TRANSCRIBED)
        if progress_value:
            progress_value(1.0, message)
        return paths.raw

    manifest.stats["current_stage"] = "transcription"
    manifest.save()
    try:
        media = probe_media(config.video_path)
        if not media.ok:
            raise RuntimeError(media.message)
        duration = float(media.details["duration"])
        windows = _clip_windows(duration)
        if not windows:
            raise RuntimeError("전사할 오디오 구간이 없습니다.")
        source_fingerprint = _source_fingerprint(config, duration)
        settings_fingerprint = _settings_fingerprint(
            language=config.direction.source, hotwords=", ".join(config.terms)
        )

        # Reuse a raw transcription only when it was produced from this exact source
        # file with the current transcription policy.
        if not overwrite:
            if not paths.raw.exists() and paths.published_raw.is_file():
                if _raw_matches(
                    paths.published_raw,
                    source_fingerprint=source_fingerprint,
                    settings_fingerprint=settings_fingerprint,
                ):
                    paths.state_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(paths.published_raw, paths.raw)
                    _log(progress, "출력 폴더의 원본 전사가 현재 영상과 일치하여 가져왔습니다.")
                else:
                    _log(
                        progress,
                        "출력 폴더의 기존 원본 전사는 현재 영상 또는 전사 정책과 달라 "
                        "사용하지 않습니다.",
                    )
            if paths.raw.exists():
                if _raw_matches(
                    paths.raw,
                    source_fingerprint=source_fingerprint,
                    settings_fingerprint=settings_fingerprint,
                ) or any(
                    _raw_matches(
                        paths.raw,
                        source_fingerprint=source_fingerprint,
                        settings_fingerprint=accepted,
                    )
                    for accepted in (
                        _accepted_raw_fingerprints(config.direction.source)
                        if not config.terms
                        else {settings_fingerprint}
                    )
                ):
                    # A finished transcription from the previous policy is kept: making
                    # a new one changes every raw_id and discards the translation.
                    _log(progress, f"기존 원본 전사를 사용합니다: {paths.raw}")
                    return finish(
                        None, duration, len(windows), "기존 전사와 화자 문맥을 확인했습니다."
                    )
                _log(progress, "기존 원본 전사가 현재 영상 또는 전사 정책과 달라 다시 만듭니다.")

        # A new raw transcription is about to be made: results published from an older
        # raw no longer describe this job, so withdraw them (moved aside, not deleted).
        retract_published_outputs(config, "원본 전사를 다시 만듭니다.")
        paths.clips_dir.mkdir(parents=True, exist_ok=True)
        checkpoints = (
            {}
            if overwrite
            else _load_checkpoints(
                paths,
                windows,
                source_fingerprint=source_fingerprint,
                settings_fingerprint=settings_fingerprint,
                progress=progress,
            )
        )
        manifest.completed_clips = sorted(checkpoints)
        missing_windows = [window for window in windows if window.index not in checkpoints]
        if not missing_windows:
            output_segments = _merge_checkpoints(
                config,
                paths,
                windows,
                checkpoints,
                duration=duration,
                source_fingerprint=source_fingerprint,
                settings_fingerprint=settings_fingerprint,
            )
            return finish(
                len(output_segments),
                duration,
                len(windows),
                f"체크포인트 {len(windows)}개를 병합했습니다.",
            )

        try:
            runtime = _resolve_runtime(config)
        except (CPUFallbackConfirmationRequired, ModelDownloadRequired, TranscriptionDeviceError):
            manifest.set_status(JobStatus.TRANSCRIPTION_BLOCKED)
            raise
        model_status = inspect_model_cache(MODEL_NAME)
        if not model_status.ready or model_status.snapshot_dir is None:
            manifest.set_status(JobStatus.TRANSCRIPTION_BLOCKED)
            raise ModelDownloadRequired(
                "large-v3 모델이 준비되어 있지 않습니다. 모델 용량과 저장 위치를 확인한 뒤 "
                "다운로드하십시오."
            )

        manifest.set_status(JobStatus.TRANSCRIBING)
        active_mark = time.monotonic()
        if progress_value:
            progress_value(
                len(checkpoints) / len(windows) * transcription_share,
                f"음성 구간을 찾는 중 · 완료 클립 {len(checkpoints)}/{len(windows)}",
            )
        _log(progress, "오디오를 읽고 Silero VAD(700/150)로 음성 구간을 찾습니다.")
        audio = _decode_audio(config.video_path)
        check_cancel()
        speech_chunks = _load_or_detect_speech(
            paths,
            audio,
            source_fingerprint=source_fingerprint,
            settings_fingerprint=settings_fingerprint,
        )
        record_active_time()
        manifest.save()
        check_cancel()

        model = None
        options = decode_options()

        def ensure_model():
            nonlocal model
            if model is None:
                    _log(
                        progress,
                        f"faster-whisper {MODEL_NAME}를 {runtime.device} "
                        f"{runtime.compute_type}로 불러옵니다.",
                    )
                    try:
                        from faster_whisper import WhisperModel

                        model = WhisperModel(
                            str(model_status.snapshot_dir),
                            device=runtime.device,
                            device_index=runtime.device_index or 0,
                            compute_type=runtime.compute_type,
                            cpu_threads=runtime.cpu_threads,
                            num_workers=1,
                        )
                    except Exception as exc:
                        raise _device_failure(runtime, exc) from exc
                    check_cancel()
            return model

        def decode(window: ClipWindow, group: dict[str, int]) -> tuple[list, Any]:
            try:
                segments, info = ensure_model().transcribe(
                    _group_audio(audio, window, group),
                    language=config.direction.source,
                    task="transcribe",
                    **({"hotwords": ", ".join(config.terms)} if config.terms else {}),
                    **options,
                )
                decoded = []
                for segment in segments:
                    check_cancel()
                    decoded.append(segment)
                return decoded, info
            except TranscriptionCancelled:
                raise
            except Exception as exc:
                raise _device_failure(runtime, exc) from exc

        def rescue_decode(rescue_spans: list[ClipWindow]) -> list[dict[str, Any]]:
            """Decode suspect windows with ordinary Whisper for completeness."""

            try:
                timestamps = [
                    value
                    for span in rescue_spans
                    for value in (span.start, span.end)
                ]
                segments, _ = ensure_model().transcribe(
                    audio,
                    language=config.direction.source,
                    task="transcribe",
                    # No hotwords here: on quiet audio without VAD, Whisper echoes the
                    # hint list for the whole window ("서수민, 문쌤" x20 in 실기 영상 F,
                    # hiding the real quiet lines it otherwise decodes correctly).
                    clip_timestamps=timestamps,
                    **options,
                )
                decoded = []
                for segment in segments:
                    check_cancel()
                    decoded.append(_absolute_segment(segment, duration))
                return decoded
            except TranscriptionCancelled:
                raise
            except Exception as exc:
                raise _device_failure(runtime, exc) from exc

        for chunk_start in range(0, len(missing_windows), RESCUE_BATCH_WINDOWS):
            chunk = missing_windows[chunk_start : chunk_start + RESCUE_BATCH_WINDOWS]
            pending: list[dict[str, Any]] = []
            for window in chunk:
                check_cancel()
                local_chunks = _window_speech_chunks(speech_chunks, window)
                speech_coverage = _window_speech_coverage(speech_chunks, window)
                speech_seconds = sum(
                    item["end"] - item["start"] for item in speech_coverage
                )
                state: dict[str, Any] = {
                    "window": window,
                    "segments": [],
                    "speech_coverage": speech_coverage,
                    "language": config.direction.source,
                    "language_probability": 1.0,
                    "used_model": False,
                    "speech_seconds": speech_seconds,
                }
                if local_chunks:
                    state["used_model"] = True
                    for group in _speech_groups(local_chunks):
                        decoded, info = decode(window, group)
                        state["language"] = str(info.language)
                        state["language_probability"] = float(info.language_probability)
                        for segment in _restore_segments(decoded, group):
                            state["segments"].append(_serialize_segment(segment, window, duration))
                dbfs = _window_dbfs(audio, window)
                state["rescue"] = {
                    "decoded": False,
                    "vad_seconds": round(speech_seconds, 3),
                    "dbfs": round(dbfs, 1),
                }
                # Gaps are measured against the lines the first pass actually wrote,
                # including lines of the previous window that run into this one.  VAD
                # coverage is not enough: VAD can mark quiet speech that Whisper then
                # leaves empty (실기 영상 F 53.5-54.8 s, "우삼겹 덮밥").
                previous = window.index - 1
                neighbour = next(
                    (item["segments"] for item in pending if item["window"].index == previous),
                    None,
                )
                if neighbour is None and previous in checkpoints:
                    neighbour = checkpoints[previous].get("segments") or []
                state["first_pass"] = [
                    segment
                    for segment in (neighbour or [])
                    if float(segment["end"]) > window.start
                ] + list(state["segments"])
                if _needs_rescue(speech_seconds, dbfs):
                    rescue_spans = [window]
                    state["rescue"]["mode"] = "whole_window"
                else:
                    rescue_spans = _rescue_spans(audio, window, state["first_pass"])
                    state["rescue"]["mode"] = "gap_triggered_window_context"
                state["rescue_spans"] = rescue_spans
                # Short isolated excerpts can make Whisper miss the very quiet line
                # that triggered this pass.  Decode the containing 30-second window
                # for linguistic context, then admit candidates only when they begin
                # inside one of the audible uncovered spans below.
                state["rescue_decode_spans"] = [window] if rescue_spans else []
                state["rescue"]["intervals"] = [
                    {"start": round(span.start, 3), "end": round(span.end, 3)}
                    for span in rescue_spans
                ]
                pending.append(state)

            rescue_states = [state for state in pending if state["rescue_spans"]]
            if rescue_states:
                decoded = rescue_decode(
                    [
                        span
                        for state in rescue_states
                        for span in state["rescue_decode_spans"]
                    ]
                )
                for state in rescue_states:
                    window = state["window"]
                    rescue_spans = state["rescue_spans"]
                    state["used_model"] = True
                    kept = sorted(
                        state["first_pass"],
                        key=lambda item: (float(item["start"]), float(item["end"])),
                    )
                    dropped: dict[str, int] = {}
                    rescued = 0
                    for candidate in decoded:
                        candidate_start = float(candidate["start"])
                        if not any(
                            span.start <= candidate_start < span.end for span in rescue_spans
                        ):
                            continue
                        reason = _rescue_reject_reason(candidate, kept, config.terms)
                        if reason:
                            dropped[reason] = dropped.get(reason, 0) + 1
                            continue
                        candidate = {**candidate, "rescue": True}
                        kept.append(candidate)
                        state["segments"].append(candidate)
                        rescued += 1
                    state["segments"].sort(
                        key=lambda item: (float(item["start"]), float(item["end"]))
                    )
                    state["rescue"].update({"decoded": True, "kept": rescued, "dropped": dropped})

            for state in pending:
                check_cancel()
                window = state["window"]
                payload = _save_checkpoint(
                    paths,
                    window,
                    state["segments"],
                    source_fingerprint=source_fingerprint,
                    settings_fingerprint=settings_fingerprint,
                    runtime=runtime if state["used_model"] else None,
                    speech_seconds=state["speech_seconds"],
                    language=state["language"],
                    language_probability=state["language_probability"],
                    rescue=state["rescue"],
                )
                checkpoints[window.index] = payload
                if window.index not in manifest.completed_clips:
                    manifest.completed_clips.append(window.index)
                manifest.completed_clips.sort()
                manifest.stats["completed_clips"] = len(checkpoints)
                record_active_time()
                manifest.save()
                _log(
                    progress,
                    f"전사 클립 {window.index + 1}/{len(windows)} 저장 · "
                    f"{window.end:.1f}/{duration:.1f}초 · 음성 {state['speech_seconds']:.1f}초",
                )
                if progress_value:
                    progress_value(
                        len(checkpoints) / len(windows) * transcription_share,
                        f"클립 {len(checkpoints)}/{len(windows)} · "
                        f"{window.end:.1f}/{duration:.1f}초",
                    )

        del model
        output_segments = _merge_checkpoints(
            config,
            paths,
            windows,
            checkpoints,
            duration=duration,
            source_fingerprint=source_fingerprint,
            settings_fingerprint=settings_fingerprint,
        )
        _log(progress, f"원본 전사 {len(output_segments)}개를 저장했습니다: {paths.raw}")
        return finish(
            len(output_segments),
            duration,
            len(windows),
            f"전사 {len(output_segments)}개를 완료했습니다.",
        )
    except TranscriptionCancelled:
        record_active_time()
        manifest.set_status(JobStatus.CANCELLED)
        raise
    except DiarizationCancelled as exc:
        record_active_time()
        manifest.set_status(JobStatus.CANCELLED)
        raise TranscriptionCancelled(str(exc)) from exc
    except DiarizationError as exc:
        record_active_time()
        manifest.set_status(JobStatus.TRANSCRIPTION_BLOCKED, error=str(exc))
        raise
    except (
        CPUFallbackConfirmationRequired,
        ModelDownloadRequired,
        TranscriptionDeviceError,
    ) as exc:
        record_active_time()
        manifest.set_status(JobStatus.TRANSCRIPTION_BLOCKED, error=str(exc))
        raise
    except BaseException as exc:
        record_active_time()
        if manifest.status != JobStatus.CANCELLED:
            manifest.set_status(JobStatus.FAILED, error=str(exc))
        raise


def _device_failure(runtime: TranscriptionRuntime, exc: BaseException) -> BaseException:
    """Only model load / inference failures are reported as device problems."""

    if runtime.accelerator == Accelerator.CUDA.value:
        return TranscriptionDeviceError(
            "CUDA 전사가 중단되었습니다. CUDA로 다시 시도하거나 CPU 전사를 명시적으로 "
            f"선택하십시오. 원인: {exc}"
        )
    return RuntimeError(f"CPU 전사가 중단되었습니다: {exc}")
