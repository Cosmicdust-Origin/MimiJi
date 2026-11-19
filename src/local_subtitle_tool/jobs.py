from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .core import JobConfig, JobPaths, JobStatus, atomic_write_json
from .diagnostics import app_data_dir

REGISTRY_SCHEMA_VERSION = 1
TERMINAL_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.COMPLETED_WITH_OMISSIONS.value,
}
JOB_SORT_MODES = frozenset({"recent", "completed", "created"})


class JobRegistryError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: str) -> float:
    """Comparable instant for ISO timestamps, including mixed UTC offsets."""

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _sort_records(records: list[JobRecord], sort_by: str) -> list[JobRecord]:
    if sort_by not in JOB_SORT_MODES:
        raise ValueError(f"지원하지 않는 작업 정렬 방식입니다: {sort_by}")
    if sort_by == "created":
        return sorted(records, key=lambda item: _timestamp(item.created_at), reverse=True)
    if sort_by == "completed":
        # Completed work comes first, newest completion first. Incomplete work follows
        # in its own recent-update order so it remains discoverable.
        return sorted(
            records,
            key=lambda item: (not item.incomplete, _timestamp(item.updated_at)),
            reverse=True,
        )
    return sorted(records, key=lambda item: _timestamp(item.updated_at), reverse=True)


def _config_fingerprint(config: JobConfig) -> str:
    canonical = json.dumps(
        config.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _job_id(config: JobConfig) -> str:
    identity = "\n".join(
        (
            str(config.output_dir).casefold(),
            str(config.video_path).casefold(),
            config.code.casefold(),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _last_completed_stage(status: str) -> str:
    if status in TERMINAL_STATUSES:
        return "완료"
    if status in {
        JobStatus.TRANSLATED.value,
        JobStatus.REVIEW_REQUIRED.value,
        JobStatus.BUILDING.value,
        JobStatus.VALIDATING.value,
    }:
        return "번역 완료"
    if status in {
        JobStatus.TRANSCRIBED.value,
        JobStatus.TRANSLATING.value,
        JobStatus.TRANSLATION_BLOCKED.value,
        JobStatus.TRANSLATION_PARTIAL.value,
        JobStatus.PARTIAL_REVIEW.value,
    }:
        return "전사 완료"
    return "준비"


@dataclass(slots=True, frozen=True)
class JobRecord:
    job_id: str
    config: dict[str, Any]
    config_fingerprint: str
    status: str
    completed_clips: tuple[int, ...]
    completed_batches: tuple[int, ...]
    active_elapsed_seconds: float
    created_at: str
    updated_at: str
    error: str | None
    registry_error: str | None

    @property
    def code(self) -> str:
        return str(self.config["code"])

    @property
    def video_path(self) -> Path:
        return Path(self.config["video_path"])

    @property
    def output_dir(self) -> Path:
        return Path(self.config["output_dir"])

    @property
    def incomplete(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def last_completed_stage(self) -> str:
        return _last_completed_stage(self.status)

    def job_config(self) -> JobConfig:
        return JobConfig.from_dict(self.config)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["completed_clips"] = list(self.completed_clips)
        data["completed_batches"] = list(self.completed_batches)
        return data

    def to_summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "code": self.code,
            "video_path": str(self.video_path),
            "output_dir": str(self.output_dir),
            "status": self.status,
            "last_completed_stage": self.last_completed_stage,
            "completed_clips": len(self.completed_clips),
            "last_completed_clip": (
                max(self.completed_clips) + 1 if self.completed_clips else None
            ),
            "completed_batches": len(self.completed_batches),
            "last_completed_batch": (
                max(self.completed_batches) + 1 if self.completed_batches else None
            ),
            "active_elapsed_seconds": self.active_elapsed_seconds,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.updated_at if not self.incomplete else "",
            "error": self.error,
            "registry_error": self.registry_error,
            "incomplete": self.incomplete,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRecord:
        config = dict(data["config"])
        JobConfig.from_dict(config)
        status = JobStatus(str(data.get("status", JobStatus.CREATED.value))).value
        completed_clips = tuple(
            sorted({int(item) for item in data.get("completed_clips", [])})
        )
        completed_batches = tuple(
            sorted({int(item) for item in data.get("completed_batches", [])})
        )
        if any(item < 0 for item in (*completed_clips, *completed_batches)):
            raise ValueError("완료 인덱스에는 음수를 사용할 수 없습니다.")
        active_elapsed_seconds = float(data.get("active_elapsed_seconds", 0.0))
        if active_elapsed_seconds < 0:
            raise ValueError("누적 작업 시간에는 음수를 사용할 수 없습니다.")
        return cls(
            job_id=str(data["job_id"]),
            config=config,
            config_fingerprint=str(data["config_fingerprint"]),
            status=status,
            completed_clips=completed_clips,
            completed_batches=completed_batches,
            active_elapsed_seconds=active_elapsed_seconds,
            created_at=str(data.get("created_at", _now())),
            updated_at=str(data.get("updated_at", _now())),
            error=str(data["error"]) if data.get("error") is not None else None,
            registry_error=(
                str(data["registry_error"])
                if data.get("registry_error") is not None
                else None
            ),
        )


@dataclass(slots=True, frozen=True)
class ResumeCheck:
    name: str
    ok: bool
    message: str

    def to_dict(self) -> dict[str, str | bool]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class ResumeInspection:
    job: JobRecord
    checks: tuple[ResumeCheck, ...]

    @property
    def can_resume(self) -> bool:
        return all(item.ok for item in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_resume": self.can_resume,
            "job": self.job.to_summary(),
            "checks": [item.to_dict() for item in self.checks],
        }


class JobRegistry:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.path = app_data_dir(state_dir) / "jobs.json"

    def _load(self) -> dict[str, JobRecord]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobRegistryError(f"작업 목록을 읽을 수 없습니다: {exc}") from exc
        if not isinstance(payload, dict):
            raise JobRegistryError("작업 목록의 최상위 값이 객체가 아닙니다.")
        try:
            version = int(payload.get("schema_version", 0))
        except (TypeError, ValueError) as exc:
            raise JobRegistryError("작업 목록 스키마 버전을 해석할 수 없습니다.") from exc
        if version != REGISTRY_SCHEMA_VERSION:
            raise JobRegistryError(f"지원하지 않는 작업 목록 형식입니다: {version}")
        raw_jobs = payload.get("jobs", [])
        if not isinstance(raw_jobs, list):
            raise JobRegistryError("작업 목록의 jobs 값이 배열이 아닙니다.")
        records: dict[str, JobRecord] = {}
        try:
            for item in raw_jobs:
                if not isinstance(item, dict):
                    raise TypeError("작업 항목이 객체가 아닙니다.")
                record = JobRecord.from_dict(item)
                records[record.job_id] = record
        except (KeyError, TypeError, ValueError) as exc:
            raise JobRegistryError(f"작업 목록 항목을 해석할 수 없습니다: {exc}") from exc
        return records

    def _save(self, records: dict[str, JobRecord]) -> None:
        ordered = sorted(records.values(), key=lambda item: item.updated_at, reverse=True)
        atomic_write_json(
            self.path,
            {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "jobs": [item.to_dict() for item in ordered],
            },
        )

    @staticmethod
    def _manifest_state(config: JobConfig) -> dict[str, Any]:
        path = JobPaths(config).manifest
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise JobRegistryError(f"작업 manifest를 읽을 수 없습니다: {exc}") from exc
        if not isinstance(payload, dict):
            raise JobRegistryError("작업 manifest의 최상위 값이 객체가 아닙니다.")
        return payload

    @staticmethod
    def _record(config: JobConfig, previous: JobRecord | None = None) -> JobRecord:
        registry_error: str | None = None
        try:
            state = JobRegistry._manifest_state(config)
        except JobRegistryError as exc:
            registry_error = str(exc)
            state = (
                {
                    "status": previous.status,
                    "completed_clips": list(previous.completed_clips),
                    "completed_batches": list(previous.completed_batches),
                    "active_elapsed_seconds": previous.active_elapsed_seconds,
                    "created_at": previous.created_at,
                    "updated_at": previous.updated_at,
                    "error": previous.error,
                }
                if previous is not None
                else {}
            )
        now = _now()
        created_at = str(
            state.get("created_at")
            or (previous.created_at if previous is not None else now)
        )
        return JobRecord(
            job_id=_job_id(config),
            config=config.to_dict(),
            config_fingerprint=_config_fingerprint(config),
            status=JobStatus(str(state.get("status", JobStatus.CREATED.value))).value,
            completed_clips=tuple(
                sorted({int(item) for item in state.get("completed_clips", [])})
            ),
            completed_batches=tuple(
                sorted({int(item) for item in state.get("completed_batches", [])})
            ),
            active_elapsed_seconds=float(state.get("active_elapsed_seconds", 0.0)),
            created_at=created_at,
            updated_at=str(state.get("updated_at") or now),
            error=str(state["error"]) if state.get("error") is not None else None,
            registry_error=registry_error,
        )

    def upsert(self, config: JobConfig) -> JobRecord:
        records = self._load()
        identifier = _job_id(config)
        record = self._record(config, records.get(identifier))
        records[identifier] = record
        self._save(records)
        return record

    def get(self, job_id: str, *, refresh: bool = True) -> JobRecord:
        records = self._load()
        try:
            record = records[job_id]
        except KeyError as exc:
            raise KeyError(f"작업을 찾을 수 없습니다: {job_id}") from exc
        if refresh:
            record = self._record(record.job_config(), record)
            records[job_id] = record
            self._save(records)
        return record

    def list_jobs(
        self,
        *,
        include_completed: bool = False,
        refresh: bool = True,
        sort_by: str = "recent",
    ) -> list[JobRecord]:
        records = self._load()
        if refresh:
            for job_id, record in list(records.items()):
                records[job_id] = self._record(record.job_config(), record)
            self._save(records)
        jobs = list(records.values())
        if not include_completed:
            jobs = [item for item in jobs if item.incomplete]
        return _sort_records(jobs, sort_by)

    def remove(self, job_id: str) -> JobRecord:
        records = self._load()
        try:
            removed = records.pop(job_id)
        except KeyError as exc:
            raise KeyError(f"작업을 찾을 수 없습니다: {job_id}") from exc
        self._save(records)
        return removed

    def inspect_resume(
        self,
        job_id: str,
        *,
        check_environment: bool = True,
    ) -> ResumeInspection:
        record = self.get(job_id)
        config = record.job_config()
        paths = JobPaths(config)
        checks = [
            ResumeCheck(
                "job_state",
                record.incomplete,
                "미완료 작업입니다." if record.incomplete else "이미 완료된 작업입니다.",
            ),
            ResumeCheck(
                "video",
                config.video_path.is_file(),
                "입력 영상을 확인했습니다."
                if config.video_path.is_file()
                else "입력 영상을 찾을 수 없습니다.",
            ),
            ResumeCheck(
                "manifest",
                paths.manifest.is_file(),
                "작업 manifest를 확인했습니다."
                if paths.manifest.is_file()
                else "작업 manifest를 찾을 수 없습니다.",
            ),
        ]
        checks.append(self._settings_check(record, paths))
        checks.append(self._checkpoint_check(record, paths))
        if check_environment:
            checks.extend(self._environment_checks(config))
        return ResumeInspection(job=record, checks=tuple(checks))

    @staticmethod
    def _settings_check(record: JobRecord, paths: JobPaths) -> ResumeCheck:
        if not paths.manifest.is_file():
            return ResumeCheck("settings", False, "설정을 비교할 manifest가 없습니다.")
        try:
            payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
            stored = JobConfig.from_dict(dict(payload["config"]))
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return ResumeCheck("settings", False, f"저장된 작업 설정을 읽을 수 없습니다: {exc}")
        matches = _config_fingerprint(stored) == record.config_fingerprint
        return ResumeCheck(
            "settings",
            matches,
            "작업 설정이 일치합니다."
            if matches
            else "작업 목록과 manifest의 설정이 다릅니다.",
        )

    @staticmethod
    def _checkpoint_check(record: JobRecord, paths: JobPaths) -> ResumeCheck:
        failures: list[str] = []
        for index in record.completed_clips:
            path = paths.clips_dir / f"{index:06d}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    valid = False
                else:
                    valid = (
                        int(payload.get("clip_index", -1)) == index
                        and isinstance(payload.get("segments"), list)
                        and bool(payload.get("source_fingerprint"))
                        and bool(payload.get("settings_fingerprint"))
                    )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                valid = False
            if not valid:
                failures.append(f"clip {index + 1}")
        for index in record.completed_batches:
            path = paths.batches_dir / f"batch_{index:04d}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    valid = False
                else:
                    target_ids = payload.get("target_ids", [])
                    item_ids = [int(item["raw_id"]) for item in payload.get("items", [])]
                    valid = (
                        bool(payload.get("fingerprint"))
                        and isinstance(target_ids, list)
                        and item_ids == [int(item) for item in target_ids]
                    )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                valid = False
            if not valid:
                failures.append(f"batch {index + 1}")
        if failures:
            return ResumeCheck(
                "checkpoints",
                False,
                "손상되거나 누락된 체크포인트가 있습니다: " + ", ".join(failures),
            )
        return ResumeCheck(
            "checkpoints",
            True,
            f"전사 {len(record.completed_clips)}개, 번역 {len(record.completed_batches)}개 "
            "체크포인트를 확인했습니다.",
        )

    @staticmethod
    def _environment_checks(config: JobConfig) -> list[ResumeCheck]:
        from .preflight import check_cpu_runtime, check_cuda
        from .providers import get_provider

        if config.accelerator == "cpu":
            runtime = check_cpu_runtime()
            runtime_name = "cpu_runtime"
        else:
            runtime = check_cuda()
            runtime_name = "cuda"
        provider = get_provider(config.provider).status()
        return [
            ResumeCheck(runtime_name, runtime.ok, runtime.message),
            ResumeCheck("provider_login", provider.ok, provider.message),
        ]
