from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

CODE_PATTERN = re.compile(r"(?i)([A-Z]{2,10}|P)-?\d{2,6}(?:-\d+)?")
MANIFEST_SCHEMA_VERSION = 5


class Accelerator(StrEnum):
    AUTO = "auto"
    CUDA = "cuda"
    CPU = "cpu"


class ComputeType(StrEnum):
    AUTO = "auto"
    FLOAT16 = "float16"
    INT8 = "int8"


class ReasoningEffort(StrEnum):
    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTRA_HIGH = "xhigh"
    MAX = "max"


class SpeedMode(StrEnum):
    STANDARD = "standard"
    FAST = "fast"


class DiarizationAlignment(StrEnum):
    BASIC = "basic"
    PRECISE = "precise"


class JobStatus(StrEnum):
    CREATED = "CREATED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSCRIPTION_BLOCKED = "TRANSCRIPTION_BLOCKED"
    TRANSCRIBED = "TRANSCRIBED"
    TRANSLATING = "TRANSLATING"
    TRANSLATION_BLOCKED = "TRANSLATION_BLOCKED"
    TRANSLATION_PARTIAL = "TRANSLATION_PARTIAL"
    TRANSLATED = "TRANSLATED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"  # legacy (schema 2); migrated to PARTIAL_REVIEW
    PARTIAL_REVIEW = "PARTIAL_REVIEW"
    BUILDING = "BUILDING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_OMISSIONS = "COMPLETED_WITH_OMISSIONS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


def _choice(value: str | StrEnum, enum_type: type[StrEnum], field_name: str) -> str:
    normalized = str(value).strip().lower()
    allowed = {item.value for item in enum_type}
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{field_name} 값은 {choices} 중 하나여야 합니다.")
    return normalized


def _index_list(value: object, field_name: str) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 값은 배열이어야 합니다.")
    indexes = [int(item) for item in value]
    if any(item < 0 for item in indexes):
        raise ValueError(f"{field_name} 값에는 음수를 사용할 수 없습니다.")
    return sorted(set(indexes))


@dataclass(slots=True, frozen=True)
class JobConfig:
    video_path: Path
    code: str
    output_dir: Path
    provider: str = "codex"
    model: str = "gpt-5.6-luna"
    # Maximum lines per translation batch. Batches are split evenly, one per lane, so
    # a whole work goes out at once (reasoning time per request barely depends on size).
    batch_size: int = 240
    context_size: int = 8
    accelerator: str = Accelerator.AUTO
    device_index: int = 0
    compute_type: str = ComputeType.AUTO
    cpu_threads: int | None = None
    reasoning_effort: str = ReasoningEffort.DEFAULT
    speed_mode: str = SpeedMode.STANDARD
    diarization_enabled: bool = False
    diarization_expected_speakers: int | None = None
    diarization_alignment: str = DiarizationAlignment.BASIC
    diarization_device: str = Accelerator.AUTO
    diarization_srt_labels: bool = False
    # "ja-ko" (Japanese audio, Korean subtitles) or "ko-ja"; see languages.py.
    language_pair: str = "ja-ko"
    # Optional proper nouns and terms ("역모, 도승지, 중전"): Whisper hint words and a
    # translation glossary so the same word is recognised and translated the same way.
    term_hints: str = ""

    def __post_init__(self) -> None:
        # The work title (historically "code"): used in output file names and reports.
        normalized_code = " ".join(self.code.split())
        if not normalized_code:
            raise ValueError("작품 제목을 입력하십시오.")
        if self.language_pair not in {"ja-ko", "ko-ja"}:
            raise ValueError("언어 방향은 ja-ko 또는 ko-ja여야 합니다.")
        provider = self.provider.strip().lower()
        if provider not in {"codex", "claude"}:
            raise ValueError("번역 제공자는 codex 또는 claude여야 합니다.")
        model = self.model.strip()
        if not model:
            raise ValueError("번역 모델을 입력해야 합니다.")
        if self.batch_size < 1:
            raise ValueError("배치 크기는 1 이상이어야 합니다.")
        if self.context_size < 0:
            raise ValueError("문맥 크기는 0 이상이어야 합니다.")
        if self.device_index < 0:
            raise ValueError("장치 인덱스는 0 이상이어야 합니다.")
        if self.cpu_threads is not None and self.cpu_threads < 1:
            raise ValueError("CPU 스레드 수는 1 이상이어야 합니다.")
        if self.diarization_expected_speakers is not None:
            if self.diarization_expected_speakers < 1:
                raise ValueError("예상 화자 수는 1 이상이어야 합니다.")
        if not isinstance(self.diarization_enabled, bool):
            raise ValueError("화자 분리 사용 여부는 참/거짓 값이어야 합니다.")
        if not isinstance(self.diarization_srt_labels, bool):
            raise ValueError("SRT 화자 표시 여부는 참/거짓 값이어야 합니다.")
        object.__setattr__(self, "code", normalized_code)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(
            self, "accelerator", _choice(self.accelerator, Accelerator, "전사 가속기")
        )
        object.__setattr__(
            self, "compute_type", _choice(self.compute_type, ComputeType, "전사 연산 형식")
        )
        object.__setattr__(
            self,
            "reasoning_effort",
            _choice(self.reasoning_effort, ReasoningEffort, "사고 수준"),
        )
        object.__setattr__(self, "speed_mode", _choice(self.speed_mode, SpeedMode, "속도 모드"))
        object.__setattr__(
            self,
            "diarization_alignment",
            _choice(self.diarization_alignment, DiarizationAlignment, "화자 정렬 방식"),
        )
        object.__setattr__(
            self,
            "diarization_device",
            _choice(self.diarization_device, Accelerator, "화자 분리 장치"),
        )
        object.__setattr__(self, "video_path", self.video_path.expanduser().resolve())
        object.__setattr__(self, "output_dir", self.output_dir.expanduser().resolve())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobConfig:
        return cls(
            video_path=Path(data["video_path"]),
            code=str(data["code"]),
            output_dir=Path(data["output_dir"]),
            provider=str(data.get("provider", "codex")),
            model=str(data.get("model", "gpt-5.6-luna")),
            batch_size=int(data.get("batch_size", 180)),
            context_size=int(data.get("context_size", 8)),
            accelerator=str(data.get("accelerator", Accelerator.AUTO)),
            device_index=int(data.get("device_index", 0)),
            compute_type=str(data.get("compute_type", ComputeType.AUTO)),
            cpu_threads=(
                int(data["cpu_threads"]) if data.get("cpu_threads") is not None else None
            ),
            reasoning_effort=str(data.get("reasoning_effort", ReasoningEffort.DEFAULT)),
            speed_mode=str(data.get("speed_mode", SpeedMode.STANDARD)),
            diarization_enabled=data.get("diarization_enabled", False),
            diarization_expected_speakers=(
                int(data["diarization_expected_speakers"])
                if data.get("diarization_expected_speakers") is not None
                else None
            ),
            diarization_alignment=str(
                data.get("diarization_alignment", DiarizationAlignment.BASIC)
            ),
            diarization_device=str(data.get("diarization_device", Accelerator.AUTO)),
            diarization_srt_labels=data.get("diarization_srt_labels", False),
            language_pair=str(data.get("language_pair", "ja-ko")),
            term_hints=str(data.get("term_hints", "") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["video_path"] = str(self.video_path)
        data["output_dir"] = str(self.output_dir)
        if data.get("language_pair") == "ja-ko":
            # Keep jobs made before the option existed byte-identical (fingerprints).
            data.pop("language_pair")
        if not data.get("term_hints"):
            data.pop("term_hints", None)
        return data

    @property
    def terms(self) -> list[str]:
        return parse_terms(self.term_hints)

    @property
    def direction(self):
        from .languages import direction

        return direction(self.language_pair)


TRANSLATION_PROFILE_FIELDS = (
    "provider",
    "model",
    "reasoning_effort",
    "speed_mode",
)


def translation_profile(config: JobConfig) -> dict[str, str]:
    """Return the execution settings that affect a translation checkpoint."""

    return {
        "provider": config.provider,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "speed_mode": config.speed_mode,
    }


def normalize_translation_profile(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    profile = {field: str(value.get(field, "")).strip() for field in TRANSLATION_PROFILE_FIELDS}
    return profile if all(profile.values()) else None


def _default_translation_resume() -> dict[str, Any]:
    return {
        "reuse_completed_batches_across_profiles": False,
        "allowed_profiles": [],
        "selected_at": None,
        "retranslate_unverified_batches": [],
    }


SESSION_RECORD_FIELDS = (
    "session_id",
    "provider",
    "execution_profile",
    "created_at",
    "last_used_at",
    "turns",
    "batches",
    "last_input_tokens",
    "usage_totals",
    "state",
    "retired_reason",
    "retired_at",
)


def _default_translation_sessions() -> dict[str, Any]:
    """Provider conversation sessions used for translation (schema 5).

    ``active`` is the session the next batch resumes; ``retired`` keeps every
    previous session with the reason it was closed (refused, rotated,
    profile_changed, resume_failed) so reports can show which session made what.
    Session ids are conversation ids, never credentials.
    """

    return {"active": None, "lanes": {}, "retired": []}


def _normalize_session_record(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not str(value.get("session_id") or "").strip():
        return None
    record = {name: value.get(name) for name in SESSION_RECORD_FIELDS}
    record["session_id"] = str(value["session_id"]).strip()
    record["execution_profile"] = normalize_translation_profile(
        value.get("execution_profile")
    )
    record["turns"] = int(value.get("turns") or 0)
    record["batches"] = [str(item) for item in value.get("batches") or []]
    return record


def _normalize_translation_sessions(value: object) -> dict[str, Any]:
    result = _default_translation_sessions()
    if not isinstance(value, dict):
        return result
    result["active"] = _normalize_session_record(value.get("active"))
    lanes = value.get("lanes")
    if isinstance(lanes, dict):
        for lane, item in lanes.items():
            record = _normalize_session_record(item)
            if record is not None:
                result["lanes"][str(lane)] = record
    result["retired"] = [
        record
        for record in (_normalize_session_record(item) for item in value.get("retired") or [])
        if record is not None
    ]
    return result


def _normalize_translation_resume(value: object) -> dict[str, Any]:
    result = _default_translation_resume()
    if not isinstance(value, dict):
        return result
    profiles: list[dict[str, str]] = []
    for item in value.get("allowed_profiles", []):
        profile = normalize_translation_profile(item)
        if profile is not None and profile not in profiles:
            profiles.append(profile)
    result.update(
        {
            "reuse_completed_batches_across_profiles": bool(
                value.get("reuse_completed_batches_across_profiles", False)
            ),
            "allowed_profiles": profiles,
            "selected_at": (
                str(value["selected_at"]) if value.get("selected_at") else None
            ),
            "retranslate_unverified_batches": _index_list(
                value.get("retranslate_unverified_batches"),
                "retranslate_unverified_batches",
            ),
        }
    )
    return result


_STATE_DIR_CACHE: dict[tuple[str, str, str, int | None], Path] = {}


def _job_folder_name(config: JobConfig) -> str:
    code = re.sub(r"[^A-Za-z0-9._-]", "_", config.code).strip("._") or "job"
    digest = hashlib.sha1(str(config.video_path).encode("utf-8")).hexdigest()[:8]
    return f"{code}-{digest}"


def _resolve_state_dir(config: JobConfig) -> Path:
    """Per-job state folder, so several videos can share one output folder.

    Up to 0.8.6 every job used ``<output>/.subtitle_job`` directly; a job whose manifest
    is still there keeps using it. Every other job lives in
    ``<output>/.subtitle_job/jobs/<CODE>-<video hash>``.
    """

    base = config.output_dir / ".subtitle_job"
    legacy_manifest = base / "manifest.json"
    try:
        stamp: int | None = legacy_manifest.stat().st_mtime_ns
    except OSError:
        stamp = None
    key = (str(config.output_dir), str(config.video_path), config.code, stamp)
    cached = _STATE_DIR_CACHE.get(key)
    if cached is not None:
        return cached
    result = base / "jobs" / _job_folder_name(config)
    if stamp is not None:
        try:
            stored = json.loads(legacy_manifest.read_text(encoding="utf-8")).get("config", {})
            if (
                Path(str(stored.get("video_path", ""))) == config.video_path
                and stored.get("code") == config.code
            ):
                result = base
        except (OSError, ValueError, AttributeError):
            pass
    _STATE_DIR_CACHE[key] = result
    return result


@dataclass(slots=True, frozen=True)
class JobPaths:
    config: JobConfig

    @property
    def state_dir(self) -> Path:
        return _resolve_state_dir(self.config)

    @property
    def batches_dir(self) -> Path:
        return self.state_dir / "batches"

    @property
    def clips_dir(self) -> Path:
        return self.state_dir / "clips"

    @property
    def manifest(self) -> Path:
        return self.state_dir / "manifest.json"

    @property
    def file_stem(self) -> str:
        """The work title made safe for Windows file names (codes like ABC-123 unchanged)."""

        return safe_file_stem(self.config.code)

    @property
    def raw_name(self) -> str:
        return f"{self.file_stem}_merged_raw_{self.config.direction.source}.json"

    @property
    def raw(self) -> Path:
        """Working raw transcription. Lives in the job state folder, not the output folder."""

        return self.state_dir / self.raw_name

    @property
    def published_raw(self) -> Path:
        """Published copy in the output folder (also where schema-2 jobs kept the raw)."""

        return self.config.output_dir / self.raw_name

    @property
    def vad(self) -> Path:
        return self.state_dir / "vad.json"

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staging"

    @property
    def superseded_dir(self) -> Path:
        return self.state_dir / "superseded"

    @property
    def ja_fix(self) -> Path:
        return self.state_dir / "ja_fix.json"

    @property
    def ko_by_index(self) -> Path:
        return self.state_dir / "ko_by_index.json"

    @property
    def decisions(self) -> Path:
        return self.state_dir / "decisions.json"

    @property
    def provider_events(self) -> Path:
        return self.state_dir / "provider_events.jsonl"

    @property
    def refused_batches(self) -> Path:
        return self.state_dir / "refused_batches.json"

    @property
    def resolutions(self) -> Path:
        return self.state_dir / "resolutions.json"

    @property
    def diarization(self) -> Path:
        return self.state_dir / "diarization.json"

    @property
    def ja_srt(self) -> Path:
        return self.config.output_dir / (
            f"{self.file_stem}_resegmented_{self.config.direction.source}.srt"
        )

    @property
    def ko_srt(self) -> Path:
        return self.config.output_dir / f"{self.file_stem}_final_{self.config.direction.target}.srt"

    @property
    def review_tsv(self) -> Path:
        return self.config.output_dir / f"{self.file_stem}_review.tsv"

    @property
    def omitted_tsv(self) -> Path:
        return self.config.output_dir / f"{self.file_stem}_omitted_candidates.tsv"

    @property
    def report(self) -> Path:
        return self.config.output_dir / f"{self.file_stem}_작업결과보고.md"

    @property
    def final_outputs(self) -> tuple[Path, ...]:
        """The six published result files in the output folder."""

        return (
            self.published_raw,
            self.ja_srt,
            self.ko_srt,
            self.review_tsv,
            self.omitted_tsv,
            self.report,
        )

    def staged(self, published: Path) -> Path:
        return self.staging_dir / published.name

    @property
    def staged_outputs(self) -> tuple[Path, ...]:
        """Build products in the state folder; published only after validation passes."""

        return (
            self.raw,
            self.staged(self.ja_srt),
            self.staged(self.ko_srt),
            self.staged(self.review_tsv),
            self.staged(self.omitted_tsv),
            self.staged(self.report),
        )


def _migrate_manifest_data(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    migrated = dict(data)
    raw_version = migrated.get("schema_version", 1)
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("manifest 스키마 버전을 해석할 수 없습니다.") from exc
    if version < 1:
        raise ValueError("manifest 스키마 버전은 1 이상이어야 합니다.")
    if version > MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            "현재 앱보다 새로운 manifest 형식입니다. 앱을 업데이트한 후 다시 시도하십시오."
        )

    changed = version != MANIFEST_SCHEMA_VERSION
    if version == 1:
        migrated.setdefault("completed_clips", [])
        migrated.setdefault("active_elapsed_seconds", 0.0)
        migrated["schema_version"] = 2
        version = 2

    if version == 2:
        # Schema 3 replaces the dead-end REVIEW_REQUIRED state with PARTIAL_REVIEW, which
        # can be resolved by automatic re-translation, user retry or manual input.
        if migrated.get("status") == JobStatus.REVIEW_REQUIRED.value:
            migrated["status"] = JobStatus.PARTIAL_REVIEW.value
        migrated["schema_version"] = 3
        version = 3

    if version == 3:
        migrated.setdefault("translation_resume", _default_translation_resume())
        migrated["schema_version"] = 4
        version = 4

    if version == 4:
        # Schema 5 records persistent provider sessions (Codex `exec resume`).
        migrated.setdefault("translation_sessions", _default_translation_sessions())
        migrated["schema_version"] = 5
        version = 5

    if version != MANIFEST_SCHEMA_VERSION:
        raise ValueError(f"지원하지 않는 manifest 스키마 버전입니다: {version}")
    return migrated, changed


@dataclass(slots=True)
class JobManifest:
    config: JobConfig
    schema_version: int = MANIFEST_SCHEMA_VERSION
    status: JobStatus = JobStatus.CREATED
    completed_batches: list[int] = field(default_factory=list)
    completed_clips: list[int] = field(default_factory=list)
    active_elapsed_seconds: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    error: str | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    translation_resume: dict[str, Any] = field(default_factory=_default_translation_resume)
    translation_sessions: dict[str, Any] = field(default_factory=_default_translation_sessions)
    # Guards in-memory state and saves when translation lanes run in parallel.
    lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    @classmethod
    def load_or_create(cls, config: JobConfig) -> JobManifest:
        paths = JobPaths(config)
        if not paths.manifest.exists():
            manifest = cls(config=config)
            manifest.save()
            return manifest
        raw_data = json.loads(paths.manifest.read_text(encoding="utf-8"))
        if not isinstance(raw_data, dict):
            raise ValueError("manifest 최상위 값은 객체여야 합니다.")
        data, migrated = _migrate_manifest_data(raw_data)
        stored_config = JobConfig.from_dict(data["config"])
        if stored_config.video_path != config.video_path or stored_config.code != config.code:
            raise ValueError("기존 작업 정보가 선택한 영상 또는 작품 제목과 일치하지 않습니다.")
        try:
            active_elapsed_seconds = float(data.get("active_elapsed_seconds", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("누적 작업 시간을 해석할 수 없습니다.") from exc
        if active_elapsed_seconds < 0:
            raise ValueError("누적 작업 시간에는 음수를 사용할 수 없습니다.")
        manifest = cls(
            config=config,
            schema_version=int(data["schema_version"]),
            status=JobStatus(data.get("status", JobStatus.CREATED)),
            completed_batches=_index_list(data.get("completed_batches"), "completed_batches"),
            completed_clips=_index_list(data.get("completed_clips"), "completed_clips"),
            active_elapsed_seconds=active_elapsed_seconds,
            created_at=str(data.get("created_at", datetime.now(UTC).isoformat())),
            updated_at=str(data.get("updated_at", datetime.now(UTC).isoformat())),
            error=data.get("error"),
            stats=dict(data.get("stats", {})),
            translation_resume=_normalize_translation_resume(
                data.get("translation_resume")
            ),
            translation_sessions=_normalize_translation_sessions(
                data.get("translation_sessions")
            ),
        )
        if migrated:
            manifest.save()
        return manifest

    def set_status(self, status: JobStatus, *, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.updated_at = datetime.now(UTC).isoformat()
        self.save()

    def add_active_seconds(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("추가할 작업 시간에는 음수를 사용할 수 없습니다.")
        with self.lock:
            self.active_elapsed_seconds += seconds
            self.updated_at = datetime.now(UTC).isoformat()
            self.save()

    def allow_translation_profile_switch(
        self, previous_config: JobConfig, new_config: JobConfig
    ) -> None:
        """Persist the user's explicit choice to reuse verified completed batches."""

        profiles: list[dict[str, str]] = []
        for profile in (
            *self.translation_resume.get("allowed_profiles", []),
            translation_profile(previous_config),
            translation_profile(new_config),
        ):
            normalized = normalize_translation_profile(profile)
            if normalized is not None and normalized not in profiles:
                profiles.append(normalized)
        self.translation_resume = {
            "reuse_completed_batches_across_profiles": True,
            "allowed_profiles": profiles,
            "selected_at": datetime.now(UTC).isoformat(),
            "retranslate_unverified_batches": [],
        }
        self.config = new_config
        self.updated_at = datetime.now(UTC).isoformat()
        self.save()

    def allow_unverified_batch_retranslation(self, batch_indexes: list[int]) -> None:
        self.translation_resume["retranslate_unverified_batches"] = sorted(
            {int(index) for index in batch_indexes if int(index) >= 0}
        )
        self.updated_at = datetime.now(UTC).isoformat()
        self.save()

    def save(self) -> None:
        with self.lock:
            self._save_locked()

    def _save_locked(self) -> None:
        paths = JobPaths(self.config)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "completed_batches": sorted(set(self.completed_batches)),
            "completed_clips": sorted(set(self.completed_clips)),
            "active_elapsed_seconds": self.active_elapsed_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "stats": self.stats,
            "translation_resume": _normalize_translation_resume(self.translation_resume),
            "translation_sessions": _normalize_translation_sessions(
                self.translation_sessions
            ),
        }
        atomic_write_json(paths.manifest, data)


def parse_terms(text: str) -> list[str]:
    """Terms separated by commas, semicolons or new lines, duplicates removed."""

    seen: list[str] = []
    for part in re.split(r"[,，、;；\n]+", text or ""):
        term = " ".join(part.split())
        if term and term not in seen:
            seen.append(term)
    return seen[:60]


_INVALID_FILE_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_file_stem(title: str) -> str:
    stem = _INVALID_FILE_CHARACTERS.sub("_", " ".join(title.split())).strip(" .")
    return stem[:80].rstrip(" .") or "subtitle"


# Release tags and bracketed notes that are not part of a title.
_TITLE_NOISE = re.compile(
    r"(?i)\b(?:2160p|1080p|720p|480p|4k|uhd|hdr|x26[45]|h\.?26[45]|hevc|avc|aac|ac3|"
    r"web[- ]?dl|webrip|bluray|bdrip|dvdrip|hdrip|hdtv|remux|10bit|8bit|dts|ddp?5\.1|"
    r"korea|korean|japan|japanese|kor|jpn|subbed|dubbed)\b"
)
_TITLE_BRACKETS = re.compile(r"[\[【(（〔][^\]】)）〕]*[\]】)）〕]")


def infer_title(path: Path) -> str:
    """A work title guessed from the file name; the user can always edit it.

    A product code such as ABC-123 is used when the name has one (short, unique and
    already how those files are named); otherwise the readable part of the name.
    """

    code = infer_code(path)
    if code:
        return code
    text = _TITLE_BRACKETS.sub(" ", path.stem)
    text = _TITLE_NOISE.sub(" ", text)
    text = re.sub(r"[._]+", " ", text)
    text = re.sub(r"\s-[A-Za-z0-9]{2,12}$", "", " ".join(text.split()))  # release group
    text = " ".join(text.split()).strip(" -")
    if not text:
        text = " ".join(path.stem.split())
    return text[:60].rstrip()


def infer_code(path: Path) -> str:
    match = CODE_PATTERN.search(path.stem)
    if not match:
        return ""
    value = match.group(0).upper()
    if "-" not in value:
        prefix = re.match(r"[A-Z]+", value)
        if prefix:
            value = f"{prefix.group(0)}-{value[len(prefix.group(0)) :]}"
    return value


REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.6)


def _replace_with_retry(source: str | Path, target: Path) -> None:
    """``os.replace`` with short retries.

    Cloud sync clients (OneDrive) and antivirus scanners briefly hold files open on
    Windows, which makes ``os.replace`` fail with ``PermissionError`` (WinError 5/32).
    """

    for delay in REPLACE_RETRY_DELAYS:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            time.sleep(delay)
    os.replace(source, target)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
