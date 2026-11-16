from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .appinfo import APP_VERSION, default_data_dir
from .core import Accelerator, ComputeType, atomic_write_json
from .model_manager import ModelCacheStatus, default_model_cache_dir, inspect_model_cache
from .preflight import (
    BLOCKED,
    ERROR,
    CheckResult,
    check_cpu_runtime,
    check_cuda,
    check_ffprobe,
    check_python_dependencies,
    claude_status,
    codex_status,
    detect_gpu_details,
    prepare_command,
    probe_media,
    resolve_executable,
    run_command,
)

DIAGNOSTIC_SCHEMA_VERSION = 1
MINIMUM_MODEL_FREE_BYTES = 4_500_000_000
MINIMUM_RUNTIME_FREE_BYTES = 512_000_000


@dataclass(slots=True, frozen=True)
class HardwareSnapshot:
    cpu_model: str
    physical_cores: int | None
    logical_cores: int
    avx2: bool | None
    total_memory_bytes: int | None
    gpu_devices: tuple[dict[str, Any], ...]
    ctranslate2_version: str | None
    python_version: str
    app_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class TranscriptionRecommendation:
    available: bool
    accelerator: str | None
    device_index: int | None
    compute_type: str | None
    cpu_threads: int | None
    requires_confirmation: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class DiagnosticReport:
    schema_version: int
    created_at: str
    hardware: HardwareSnapshot
    hardware_fingerprint: str
    checks: list[CheckResult]
    recommendation: TranscriptionRecommendation
    selected_provider: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "hardware": self.hardware.to_dict(),
            "hardware_fingerprint": self.hardware_fingerprint,
            "checks": [item.to_dict() for item in self.checks],
            "recommendation": self.recommendation.to_dict(),
            "selected_provider": self.selected_provider,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiagnosticReport:
        schema_version = int(data.get("schema_version", 0))
        if schema_version != DIAGNOSTIC_SCHEMA_VERSION:
            raise ValueError("지원하지 않는 환경 진단 결과 형식입니다.")
        hardware_data = dict(data["hardware"])
        hardware = HardwareSnapshot(
            cpu_model=str(hardware_data["cpu_model"]),
            physical_cores=(
                int(hardware_data["physical_cores"])
                if hardware_data.get("physical_cores") is not None
                else None
            ),
            logical_cores=int(hardware_data["logical_cores"]),
            avx2=hardware_data.get("avx2"),
            total_memory_bytes=(
                int(hardware_data["total_memory_bytes"])
                if hardware_data.get("total_memory_bytes") is not None
                else None
            ),
            gpu_devices=tuple(dict(item) for item in hardware_data.get("gpu_devices", [])),
            ctranslate2_version=hardware_data.get("ctranslate2_version"),
            python_version=str(hardware_data["python_version"]),
            app_version=str(hardware_data["app_version"]),
        )
        checks = [CheckResult(**dict(item)) for item in data.get("checks", [])]
        recommendation = TranscriptionRecommendation(**dict(data["recommendation"]))
        return cls(
            schema_version=schema_version,
            created_at=str(data["created_at"]),
            hardware=hardware,
            hardware_fingerprint=str(data["hardware_fingerprint"]),
            checks=checks,
            recommendation=recommendation,
            selected_provider=str(data["selected_provider"]),
        )

    def selected_provider_ready(self) -> bool:
        return any(
            item.name == self.selected_provider and item.ok
            for item in self.checks
        )

    def ready_to_start(self) -> bool:
        by_name = {item.name: item for item in self.checks}
        return (
            bool(by_name.get("ffprobe") and by_name["ffprobe"].ok)
            and bool(by_name.get("python_dependencies") and by_name["python_dependencies"].ok)
            and bool(by_name.get("model_large_v3") and by_name["model_large_v3"].ok)
            and bool(by_name.get("storage") and by_name["storage"].ok)
            and self.recommendation.available
            and self.selected_provider_ready()
        )


@dataclass(slots=True, frozen=True)
class QuickDiagnosticResult:
    checked_at: str
    hardware_fingerprint: str
    full_diagnostic_required: bool
    reasons: tuple[str, ...]
    recommendation: TranscriptionRecommendation


def _app_version() -> str:
    # _version.py is the single source of truth (packaged builds carry no dist-info).
    return APP_VERSION


def app_data_dir(override: Path | None = None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    return default_data_dir()


def diagnostic_report_path(override: Path | None = None) -> Path:
    return app_data_dir(override) / "diagnostics.json"


def _windows_cpu_details() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    powershell = resolve_executable("pwsh") or resolve_executable("powershell")
    if powershell is None:
        return {}
    script = (
        "Get-CimInstance Win32_Processor | "
        "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | "
        "ConvertTo-Json -Compress"
    )
    result = run_command(
        prepare_command(powershell, "-NoLogo", "-NoProfile", "-Command", script),
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    items = payload if isinstance(payload, list) else [payload]
    valid = [item for item in items if isinstance(item, dict)]
    if not valid:
        return {}
    return {
        "name": str(valid[0].get("Name", "")).strip(),
        "physical_cores": sum(int(item.get("NumberOfCores", 0)) for item in valid),
        "logical_cores": sum(int(item.get("NumberOfLogicalProcessors", 0)) for item in valid),
    }


def _avx2_available() -> bool | None:
    if os.name != "nt":
        return None
    try:
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def _total_memory_bytes() -> int | None:
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return int(status.ullTotalPhys)
        except (AttributeError, OSError):
            return None
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        return page_size * pages
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def collect_hardware_snapshot() -> HardwareSnapshot:
    windows_cpu = _windows_cpu_details()
    logical_cores = int(windows_cpu.get("logical_cores") or os.cpu_count() or 1)
    cpu_model = str(
        windows_cpu.get("name")
        or platform.processor()
        or os.environ.get("PROCESSOR_IDENTIFIER")
        or platform.machine()
        or "unknown"
    ).strip()
    gpu_details = detect_gpu_details()
    gpu_devices = tuple(dict(item) for item in gpu_details.get("gpu_devices", []))
    try:
        import ctranslate2

        ctranslate2_version = str(getattr(ctranslate2, "__version__", "installed"))
    except ImportError:
        ctranslate2_version = None
    return HardwareSnapshot(
        cpu_model=cpu_model,
        physical_cores=(
            int(windows_cpu["physical_cores"])
            if windows_cpu.get("physical_cores")
            else None
        ),
        logical_cores=logical_cores,
        avx2=_avx2_available(),
        total_memory_bytes=_total_memory_bytes(),
        gpu_devices=gpu_devices,
        ctranslate2_version=ctranslate2_version,
        python_version=platform.python_version(),
        app_version=_app_version(),
    )


def hardware_fingerprint(snapshot: HardwareSnapshot) -> str:
    stable_data = {
        "cpu_model": snapshot.cpu_model,
        "logical_cores": snapshot.logical_cores,
        "avx2": snapshot.avx2,
        "total_memory_bytes": snapshot.total_memory_bytes,
        "gpu_devices": snapshot.gpu_devices,
        "ctranslate2_version": snapshot.ctranslate2_version,
        "python_version": snapshot.python_version,
        "app_version": snapshot.app_version,
    }
    serialized = json.dumps(stable_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def recommended_cpu_threads(logical_cores: int) -> int:
    return max(1, logical_cores - 1)


def recommend_transcription(
    cuda: CheckResult,
    cpu: CheckResult,
    *,
    logical_cores: int,
) -> TranscriptionRecommendation:
    cuda_types = set(cuda.details.get("supported_compute_types", []))
    if cuda.ok and "float16" in cuda_types:
        return TranscriptionRecommendation(
            available=True,
            accelerator=Accelerator.CUDA.value,
            device_index=0,
            compute_type=ComputeType.FLOAT16.value,
            cpu_threads=None,
            requires_confirmation=False,
            reason="CUDA 0에서 large-v3 float16 전사를 권장합니다.",
        )
    cpu_types = set(cpu.details.get("supported_compute_types", []))
    if cpu.ok and "int8" in cpu_types:
        return TranscriptionRecommendation(
            available=True,
            accelerator=Accelerator.CPU.value,
            device_index=None,
            compute_type=ComputeType.INT8.value,
            cpu_threads=recommended_cpu_threads(logical_cores),
            requires_confirmation=True,
            reason="CUDA를 사용할 수 없어 CPU int8 전사를 사용할 수 있습니다.",
        )
    return TranscriptionRecommendation(
        available=False,
        accelerator=None,
        device_index=None,
        compute_type=None,
        cpu_threads=None,
        requires_confirmation=True,
        reason="CUDA float16과 CPU int8 전사를 모두 사용할 수 없습니다.",
    )


def _capture(name: str, priority: int, check) -> CheckResult:
    try:
        return check()
    except Exception as exc:
        return CheckResult(
            name=name,
            ok=False,
            message=f"진단 중 오류가 발생했습니다: {exc}",
            details={"error_type": type(exc).__name__, "error": str(exc)},
            status=ERROR,
            priority=priority,
        )


def _storage_check(cache_dir: Path, *, model_ready: bool) -> CheckResult:
    target = cache_dir
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
    except OSError as exc:
        return CheckResult(
            "storage",
            False,
            "모델 저장 공간을 확인할 수 없습니다.",
            {"path": str(cache_dir), "error": str(exc)},
            status=ERROR,
            priority=35,
        )
    minimum_free_bytes = (
        MINIMUM_RUNTIME_FREE_BYTES if model_ready else MINIMUM_MODEL_FREE_BYTES
    )
    details = {
        "path": str(cache_dir),
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "minimum_free_bytes": minimum_free_bytes,
        "model_ready": model_ready,
    }
    if usage.free < minimum_free_bytes:
        return CheckResult(
            "storage",
            False,
            "large-v3 모델을 준비하기 위한 디스크 여유 공간이 부족합니다.",
            details,
            status=BLOCKED,
            priority=35,
        )
    return CheckResult(
        "storage",
        True,
        "전사 모델을 저장할 디스크 공간이 충분합니다.",
        details,
        priority=35,
    )


def _model_check(status: ModelCacheStatus) -> CheckResult:
    details = status.to_dict()
    if status.ready:
        return CheckResult(
            "model_large_v3",
            True,
            "faster-whisper large-v3 모델이 준비되어 있습니다.",
            details,
            priority=37,
        )
    return CheckResult(
        "model_large_v3",
        False,
        "faster-whisper large-v3 모델 다운로드가 필요합니다.",
        details,
        status=BLOCKED,
        priority=37,
    )


def run_full_diagnostic(
    *,
    video_path: Path | None = None,
    selected_provider: str = "codex",
    state_dir: Path | None = None,
    model_cache_dir: Path | None = None,
    persist: bool = True,
) -> DiagnosticReport:
    if selected_provider not in {"codex", "claude"}:
        raise ValueError("번역 제공자는 codex 또는 claude여야 합니다.")
    hardware = collect_hardware_snapshot()
    cache_dir = model_cache_dir or default_model_cache_dir()
    model_status = inspect_model_cache(cache_dir=cache_dir)
    checks: list[CheckResult] = []
    if video_path is not None:
        checks.append(_capture("media", 10, lambda: probe_media(video_path)))
    checks.extend(
        [
            _capture("ffprobe", 20, check_ffprobe),
            _capture("python_dependencies", 30, check_python_dependencies),
            _capture(
                "storage",
                35,
                lambda: _storage_check(cache_dir, model_ready=model_status.ready),
            ),
            _capture(
                "model_large_v3",
                37,
                lambda: _model_check(model_status),
            ),
            _capture("cuda", 40, check_cuda),
            _capture("cpu_runtime", 45, check_cpu_runtime),
            _capture("codex", 50, codex_status),
            _capture("claude", 60, claude_status),
        ]
    )
    by_name = {item.name: item for item in checks}
    recommendation = recommend_transcription(
        by_name["cuda"],
        by_name["cpu_runtime"],
        logical_cores=hardware.logical_cores,
    )
    report = DiagnosticReport(
        schema_version=DIAGNOSTIC_SCHEMA_VERSION,
        created_at=datetime.now(UTC).isoformat(),
        hardware=hardware,
        hardware_fingerprint=hardware_fingerprint(hardware),
        checks=checks,
        recommendation=recommendation,
        selected_provider=selected_provider,
    )
    if persist:
        atomic_write_json(diagnostic_report_path(state_dir), report.to_dict())
    return report


def load_diagnostic_report(state_dir: Path | None = None) -> DiagnosticReport | None:
    path = diagnostic_report_path(state_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return DiagnosticReport.from_dict(data)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def run_quick_diagnostic(*, state_dir: Path | None = None) -> QuickDiagnosticResult:
    cached = load_diagnostic_report(state_dir)
    hardware = collect_hardware_snapshot()
    fingerprint = hardware_fingerprint(hardware)
    cuda = _capture("cuda", 40, check_cuda)
    cpu = _capture("cpu_runtime", 45, check_cpu_runtime)
    recommendation = recommend_transcription(cuda, cpu, logical_cores=hardware.logical_cores)
    reasons: list[str] = []
    if cached is None:
        reasons.append("저장된 전체 진단 결과가 없습니다.")
    elif cached.hardware_fingerprint != fingerprint:
        reasons.append("하드웨어, 드라이버, 런타임 또는 앱 버전이 변경되었습니다.")
    if resolve_executable("ffprobe") is None:
        reasons.append("ffprobe 실행 파일을 찾을 수 없습니다.")
    if not recommendation.available:
        reasons.append("사용 가능한 전사 연산 장치가 없습니다.")
    return QuickDiagnosticResult(
        checked_at=datetime.now(UTC).isoformat(),
        hardware_fingerprint=fingerprint,
        full_diagnostic_required=bool(reasons),
        reasons=tuple(reasons),
        recommendation=recommendation,
    )
