from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .core import JobConfig, JobPaths, atomic_write_json, load_json
from .installers import is_frozen

MODEL_ID = "pyannote/speaker-diarization-community-1"
DIARIZATION_SCHEMA_VERSION = 1
ProgressCallback = Callable[[str], None]
ProgressValueCallback = Callable[[float, str], None]


class DiarizationError(RuntimeError):
    pass


class DiarizationCancelled(DiarizationError):
    pass


@dataclass(slots=True, frozen=True)
class DiarizationTurn:
    start: float
    end: float
    speaker: str

    def to_dict(self) -> dict[str, str | float]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class DiarizationStatus:
    runtime_ready: bool
    authenticated: bool
    model_ready: bool
    ready: bool
    message: str


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _huggingface_token() -> str | None:
    try:
        from huggingface_hub import get_token

        return get_token()
    except (ImportError, OSError, RuntimeError):
        return None


def _cache_not_found_error() -> type[Exception]:
    try:
        from huggingface_hub.errors import CacheNotFound
    except ImportError:  # pragma: no cover - older huggingface_hub layouts
        try:
            from huggingface_hub.utils import CacheNotFound  # type: ignore[no-redef]
        except ImportError:
            return OSError
    return CacheNotFound


def _scan_hf_cache():
    """Return the Hugging Face cache scan, or None when the cache does not exist yet.

    A fresh Windows account has no ``~/.cache/huggingface/hub`` until the first model
    download, and ``scan_cache_dir`` raises ``CacheNotFound`` (not an ``OSError``) then.
    """

    from huggingface_hub import scan_cache_dir

    try:
        return scan_cache_dir()
    except _cache_not_found_error():
        return None


def _model_cached() -> bool:
    try:
        cache = _scan_hf_cache()
    except (ImportError, OSError, RuntimeError):
        return False
    if cache is None:
        return False
    return any(repo.repo_id == MODEL_ID and repo.revisions for repo in cache.repos)


def inspect_diarization_status() -> DiarizationStatus:
    runtime_ready = _module_exists("torch") and _module_exists("pyannote.audio")
    authenticated = bool(_huggingface_token())
    model_ready = _model_cached()
    ready = runtime_ready and authenticated and model_ready
    if not runtime_ready and is_frozen():
        message = (
            "이 배포판에는 화자 분리가 포함되어 있지 않습니다. "
            "MimiJi-Diarization 배포판을 사용하십시오."
        )
    elif not runtime_ready:
        message = "선택 구성 요소를 설치해야 합니다."
    elif not authenticated:
        message = "Hugging Face 로그인이 필요합니다."
    elif not model_ready:
        message = "community-1 모델을 받아야 합니다."
    else:
        message = "community-1 화자 분리를 사용할 수 있습니다."
    return DiarizationStatus(
        runtime_ready=runtime_ready,
        authenticated=authenticated,
        model_ready=model_ready,
        ready=ready,
        message=message,
    )


def _load_pipeline():
    status = inspect_diarization_status()
    if not status.runtime_ready:
        raise DiarizationError(status.message)
    token = _huggingface_token()
    if not token:
        raise DiarizationError(
            "Hugging Face 로그인이 필요합니다. 모델 이용 조건에 동의한 뒤 로그인하십시오."
        )
    os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(MODEL_ID, token=token)
    if pipeline is None:
        raise DiarizationError(
            "community-1 모델을 불러오지 못했습니다. 이용 조건 동의와 로그인을 확인하십시오."
        )
    return pipeline


def download_diarization_model() -> Path:
    pipeline = _load_pipeline()
    del pipeline
    try:
        cache = _scan_hf_cache()
        if cache is None:
            raise StopIteration
        repo = next(item for item in cache.repos if item.repo_id == MODEL_ID)
        revision = max(repo.revisions, key=lambda item: item.last_modified)
        return Path(revision.snapshot_path)
    except (ImportError, OSError, RuntimeError, StopIteration) as exc:
        raise DiarizationError("화자 분리 모델 캐시를 확인하지 못했습니다.") from exc


def _turns_from_annotation(annotation: Any) -> list[DiarizationTurn]:
    turns: list[DiarizationTurn] = []
    if hasattr(annotation, "itertracks"):
        iterator: Iterable[Any] = annotation.itertracks(yield_label=True)
        for turn, _track, speaker in iterator:
            turns.append(
                DiarizationTurn(float(turn.start), float(turn.end), str(speaker))
            )
    else:
        for turn, speaker in annotation:
            turns.append(
                DiarizationTurn(float(turn.start), float(turn.end), str(speaker))
            )
    return sorted(turns, key=lambda item: (item.start, item.end, item.speaker))


def _accepts_hook(pipeline: Any) -> bool:
    import inspect

    for target in (getattr(pipeline, "apply", None), pipeline):
        if target is None:
            continue
        try:
            parameters = inspect.signature(target).parameters
        except (TypeError, ValueError):
            continue
        if "hook" in parameters:
            return True
    return False


def run_diarization(
    config: JobConfig,
    *,
    progress: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[list[DiarizationTurn], str]:
    if cancel_event and cancel_event.is_set():
        raise DiarizationCancelled("사용자 요청으로 화자 분리를 중지했습니다.")
    if progress:
        progress("community-1 화자 분리 모델을 불러옵니다.")
    pipeline = _load_pipeline()

    import torch

    requested = config.diarization_device
    if requested == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        device = requested
    if device == "cuda" and not torch.cuda.is_available():
        raise DiarizationError(
            "PyTorch에서 CUDA를 사용할 수 없습니다. 화자 분리 장치를 CPU로 선택하십시오."
        )
    if device == "cpu" and config.cpu_threads:
        torch.set_num_threads(config.cpu_threads)
    pipeline.to(torch.device(device))
    if progress:
        progress(f"community-1 화자 분리를 {device}에서 실행합니다.")
    options: dict[str, Any] = {}
    if config.diarization_expected_speakers is not None:
        options["num_speakers"] = config.diarization_expected_speakers
    def cancel_hook(*_args: Any, **_kwargs: Any) -> None:
        # pyannote calls the hook between pipeline steps and batches; raising here is
        # the only way to stop a long diarization run early.
        if cancel_event and cancel_event.is_set():
            raise DiarizationCancelled("사용자 요청으로 화자 분리를 중지했습니다.")

    if _accepts_hook(pipeline):
        options["hook"] = cancel_hook
    output = pipeline(str(config.video_path), **options)
    if cancel_event and cancel_event.is_set():
        raise DiarizationCancelled("사용자 요청으로 화자 분리를 중지했습니다.")
    annotation = getattr(output, "exclusive_speaker_diarization", None)
    if annotation is None:
        annotation = getattr(output, "speaker_diarization", output)
    turns = _turns_from_annotation(annotation)
    if not turns:
        raise DiarizationError("화자 발화 구간을 찾지 못했습니다.")
    return turns, device


def _overlap(start: float, end: float, turn: DiarizationTurn) -> float:
    return max(0.0, min(end, turn.end) - max(start, turn.start))


def _speaker_for_interval(
    start: float,
    end: float,
    turns: list[DiarizationTurn],
) -> str | None:
    if end <= start or not turns:
        return None
    scores: dict[str, float] = {}
    for turn in turns:
        overlap = _overlap(start, end, turn)
        if overlap:
            scores[turn.speaker] = scores.get(turn.speaker, 0.0) + overlap
    if scores:
        return max(scores, key=lambda speaker: (scores[speaker], speaker))
    midpoint = (start + end) / 2
    nearest = min(
        turns,
        key=lambda turn: min(abs(midpoint - turn.start), abs(midpoint - turn.end)),
    )
    distance = min(abs(midpoint - nearest.start), abs(midpoint - nearest.end))
    return nearest.speaker if distance <= 1.5 else None


def _smooth_word_speakers(words: list[dict[str, Any]]) -> None:
    if len(words) < 3:
        return
    labels = [word.get("speaker") for word in words]
    for index in range(1, len(labels) - 1):
        if labels[index - 1] == labels[index + 1] and labels[index] != labels[index - 1]:
            labels[index] = labels[index - 1]
    for word, label in zip(words, labels, strict=True):
        if label is None:
            word.pop("speaker", None)
        else:
            word["speaker"] = label


def assign_speakers(
    segments: list[dict[str, Any]],
    turns: list[DiarizationTurn],
    *,
    alignment: str,
) -> list[dict[str, Any]]:
    if alignment not in {"basic", "precise"}:
        raise ValueError("화자 정렬 방식은 basic 또는 precise여야 합니다.")
    assigned: list[dict[str, Any]] = []
    for source in segments:
        segment = dict(source)
        words = [dict(item) for item in source.get("words", [])]
        if alignment == "precise" and words:
            for word in words:
                speaker = _speaker_for_interval(
                    float(word["start"]),
                    float(word["end"]),
                    turns,
                )
                if speaker is not None:
                    word["speaker"] = speaker
            _smooth_word_speakers(words)
            durations: dict[str, float] = {}
            for word in words:
                speaker = word.get("speaker")
                if speaker is None:
                    continue
                duration = max(0.001, float(word["end"]) - float(word["start"]))
                durations[str(speaker)] = durations.get(str(speaker), 0.0) + duration
            speaker = (
                max(durations, key=lambda label: (durations[label], label))
                if durations
                else None
            )
            segment["words"] = words
        else:
            speaker = None
        if speaker is None:
            speaker = _speaker_for_interval(
                float(segment["start"]),
                float(segment["end"]),
                turns,
            )
        if speaker is None:
            segment.pop("speaker", None)
        else:
            segment["speaker"] = speaker
        assigned.append(segment)
    return assigned


def _strip_speakers(payload: dict[str, Any]) -> bool:
    changed = False
    for segment in payload.get("segments", []):
        if "speaker" in segment:
            segment.pop("speaker", None)
            changed = True
        for word in segment.get("words", []):
            if "speaker" in word:
                word.pop("speaker", None)
                changed = True
    meta = payload.setdefault("meta", {})
    if "diarization" in meta:
        meta.pop("diarization", None)
        changed = True
    return changed


def _fingerprint(config: JobConfig, raw: dict[str, Any]) -> str:
    payload = {
        "schema_version": DIARIZATION_SCHEMA_VERSION,
        "model": MODEL_ID,
        "source_fingerprint": raw.get("meta", {}).get("source_fingerprint"),
        "expected_speakers": config.diarization_expected_speakers,
        "alignment": config.diarization_alignment,
        "device": config.diarization_device,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def apply_diarization(
    config: JobConfig,
    *,
    progress: ProgressCallback | None = None,
    progress_value: ProgressValueCallback | None = None,
    cancel_event: threading.Event | None = None,
    runner: Callable[..., tuple[list[DiarizationTurn], str]] = run_diarization,
) -> Path:
    paths = JobPaths(config)
    raw = load_json(paths.raw)
    if not config.diarization_enabled:
        if _strip_speakers(raw):
            atomic_write_json(paths.raw, raw)
        return paths.raw

    if cancel_event and cancel_event.is_set():
        raise DiarizationCancelled("사용자 요청으로 화자 분리를 중지했습니다.")
    fingerprint = _fingerprint(config, raw)
    turns: list[DiarizationTurn] = []
    device = "unknown"
    if paths.diarization.is_file():
        try:
            cached = load_json(paths.diarization)
            if cached.get("fingerprint") == fingerprint:
                turns = [DiarizationTurn(**dict(item)) for item in cached.get("turns", [])]
                device = str(cached.get("device", "unknown"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            turns = []
    if turns:
        if progress:
            progress(f"저장된 화자 구간 {len(turns)}개를 재사용합니다.")
    else:
        if progress_value:
            progress_value(0.0, "화자 분리 모델을 준비하고 있습니다.")
        try:
            turns, device = runner(config, progress=progress, cancel_event=cancel_event)
        except DiarizationError:
            raise
        except Exception as exc:
            # torch/pyannote/torchcodec/Hugging Face errors are diarization problems,
            # never transcription-device problems.
            raise DiarizationError(f"화자 분리를 실행하지 못했습니다: {exc}") from exc
        atomic_write_json(
            paths.diarization,
            {
                "schema_version": DIARIZATION_SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "model": MODEL_ID,
                "device": device,
                "turns": [item.to_dict() for item in turns],
            },
        )
    segments = assign_speakers(
        [dict(item) for item in raw.get("segments", [])],
        turns,
        alignment=config.diarization_alignment,
    )
    speakers = sorted(
        {str(item["speaker"]) for item in segments if item.get("speaker") is not None}
    )
    raw["segments"] = segments
    raw.setdefault("meta", {})["diarization"] = {
        "enabled": True,
        "model": MODEL_ID,
        "device": device,
        "alignment": config.diarization_alignment,
        "expected_speakers": config.diarization_expected_speakers,
        "detected_speakers": speakers,
        "turns": len(turns),
        "srt_labels": config.diarization_srt_labels,
    }
    atomic_write_json(paths.raw, raw)
    if progress_value:
        progress_value(1.0, f"화자 {len(speakers)}명 · 발화 구간 {len(turns)}개를 연결했습니다.")
    if progress:
        progress(f"화자 {len(speakers)}명의 문맥을 원본 전사에 연결했습니다.")
    return paths.raw
