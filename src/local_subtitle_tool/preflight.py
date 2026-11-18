from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

READY = "ready"
MISSING = "missing"
LOGIN_REQUIRED = "login_required"
BLOCKED = "blocked"
ERROR = "error"


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    status: str = READY
    priority: int = 100
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _windows_candidates(name: str) -> list[Path]:
    if os.name != "nt":
        return []
    try:
        home = Path.home()
    except RuntimeError:
        home = Path.cwd()
    local_app_data = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    app_data = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates: dict[str, list[Path]] = {
        "codex": [],
        "claude": [home / ".local" / "bin" / "claude.exe"],
        "nvidia-smi": [windows / "System32" / "nvidia-smi.exe"],
        "ffprobe": [],
    }
    if name == "codex":
        codex_bin = local_app_data / "OpenAI" / "Codex" / "bin"
        native_versions = list(codex_bin.glob("*/codex.exe"))
        native_versions.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        candidates[name].extend(native_versions)
        candidates[name].extend(
            [
                app_data / "npm" / "codex.cmd",
                app_data / "npm" / "codex.ps1",
            ]
        )
    if name == "ffprobe":
        package_root = local_app_data / "Microsoft" / "WinGet" / "Packages"
        candidates[name].extend(sorted(package_root.glob("Gyan.FFmpeg_*/ffmpeg-*/bin/ffprobe.exe")))
    return candidates.get(name, [])


def resolve_executable(name: str) -> Path | None:
    detected = shutil.which(name)
    if os.name == "nt" and name == "codex":
        if detected and Path(detected).suffix.lower() == ".exe":
            return Path(detected).resolve()
        for candidate in _windows_candidates(name):
            if candidate.is_file():
                return candidate.resolve()
    if detected:
        return Path(detected).resolve()
    for candidate in _windows_candidates(name):
        if candidate.is_file():
            return candidate.resolve()
    return None


CMD_UNSAFE_CHARACTERS = frozenset("\r\n%!^&|<>")


class UnsafeCommandArgument(ValueError):
    pass


def prepare_command(executable: Path | str, *arguments: str) -> list[str]:
    path = Path(executable)
    suffix = path.suffix.lower()
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        # cmd.exe re-parses the command line: newlines end the command and these
        # characters are interpreted, so such arguments cannot be passed safely.
        for argument in arguments:
            if CMD_UNSAFE_CHARACTERS.intersection(argument):
                raise UnsafeCommandArgument(
                    f"{path.name}(npm 설치본)를 거쳐서는 줄바꿈이나 %, !, ^, &, |, <, > "
                    "문자가 들어간 인자(예: 작업 폴더 경로)를 안전하게 전달할 수 없습니다. "
                    "작업 폴더 이름에서 해당 문자를 빼거나, 공식 설치 프로그램의 "
                    "실행 파일(.exe)을 사용하십시오."
                )
        command_processor = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        return [command_processor, "/d", "/s", "/c", str(path), *arguments]
    if os.name == "nt" and suffix == ".ps1":
        powershell = resolve_executable("pwsh") or resolve_executable("powershell")
        if powershell is None:
            powershell = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
        return [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
            *arguments,
        ]
    return [str(path), *arguments]


def run_command(
    command: list[str], *, timeout: int = 30, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _first_output_line(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stdout or result.stderr).strip()
    return text.splitlines()[0].strip() if text else "unknown"


def check_executable(name: str) -> CheckResult:
    path = resolve_executable(name)
    if not path:
        return CheckResult(
            name=name,
            ok=False,
            message=f"{name} 실행 파일을 찾을 수 없습니다.",
            status=MISSING,
        )
    return CheckResult(
        name=name,
        ok=True,
        message=f"{name} 실행 파일을 확인했습니다.",
        details={"path": str(path)},
    )


def check_ffprobe() -> CheckResult:
    path = resolve_executable("ffprobe")
    if not path:
        return CheckResult(
            "ffprobe",
            False,
            "FFmpeg/ffprobe가 설치되어 있지 않습니다.",
            status=MISSING,
            priority=20,
        )
    result = run_command(prepare_command(path, "-version"), timeout=15)
    if result.returncode != 0:
        return CheckResult(
            "ffprobe",
            False,
            "ffprobe를 찾았지만 실행하지 못했습니다.",
            {"path": str(path), "stderr": result.stderr.strip()},
            status=ERROR,
            priority=20,
        )
    return CheckResult(
        "ffprobe",
        True,
        "FFmpeg/ffprobe를 사용할 수 있습니다.",
        {"path": str(path), "version": _first_output_line(result)},
        priority=20,
    )


def probe_media(video_path: Path) -> CheckResult:
    if not video_path.is_file():
        return CheckResult(
            "media",
            False,
            "입력 영상 파일을 찾을 수 없습니다.",
            {"path": str(video_path)},
            status=MISSING,
            priority=10,
        )
    ffprobe = resolve_executable("ffprobe")
    if not ffprobe:
        return CheckResult(
            "media",
            False,
            "ffprobe가 없어 영상의 오디오를 확인할 수 없습니다.",
            {"path": str(video_path)},
            status=BLOCKED,
            priority=10,
        )
    command = prepare_command(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,channels,sample_rate",
        "-of",
        "json",
        str(video_path),
    )
    result = run_command(command, timeout=60)
    if result.returncode != 0:
        return CheckResult(
            "media",
            False,
            "ffprobe 실행에 실패했습니다.",
            {"path": str(video_path), "stderr": result.stderr.strip()},
            status=ERROR,
            priority=10,
        )
    try:
        payload = json.loads(result.stdout)
        duration = float(payload.get("format", {}).get("duration", 0.0))
        audio_streams = [
            item for item in payload.get("streams", []) if item.get("codec_type") == "audio"
        ]
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return CheckResult(
            "media",
            False,
            "ffprobe 결과를 해석할 수 없습니다.",
            {"path": str(video_path), "error": str(exc)},
            status=ERROR,
            priority=10,
        )
    if not audio_streams:
        return CheckResult(
            "media",
            False,
            "영상에 오디오 스트림이 없습니다.",
            {"path": str(video_path), "duration": duration},
            status=BLOCKED,
            priority=10,
        )
    return CheckResult(
        "media",
        True,
        f"영상 길이 {duration:.1f}초와 오디오 스트림 {len(audio_streams)}개를 확인했습니다.",
        {"path": str(video_path), "duration": duration, "audio_streams": audio_streams},
        priority=10,
    )


def detect_gpu_details() -> dict[str, Any]:
    details: dict[str, Any] = {}
    nvidia_smi = resolve_executable("nvidia-smi")
    if nvidia_smi:
        result = run_command(
            prepare_command(
                nvidia_smi,
                "--query-gpu=index,name,driver_version,memory.total",
                "--format=csv,noheader",
            ),
            timeout=15,
        )
        if result.returncode == 0:
            rows = [row for row in csv.reader(result.stdout.splitlines()) if row]
            devices = []
            for row in rows:
                if len(row) < 4:
                    continue
                memory_text = row[3].strip()
                memory_digits = "".join(
                    character for character in memory_text if character.isdigit()
                )
                devices.append(
                    {
                        "index": int(row[0].strip()),
                        "name": row[1].strip(),
                        "driver_version": row[2].strip(),
                        "memory_total_mib": int(memory_digits) if memory_digits else None,
                    }
                )
            details["gpu_devices"] = devices
            details["gpus"] = [", ".join(item.strip() for item in row) for row in rows]
            details["nvidia_smi"] = str(nvidia_smi)
    return details


# CTranslate2 4.5+ is built against CUDA 12. Its wheel ships cuDNN but not cuBLAS,
# which must come from a CUDA 12 runtime on the DLL search path.
CUDA_RUNTIME_LIBRARIES = ("cublas64_12.dll", "cublasLt64_12.dll")


def _load_library(name: str) -> None:
    import ctypes

    # winmode=0 uses the standard LoadLibrary search order (including PATH), which is
    # how CTranslate2 itself resolves cuBLAS at model load time.
    ctypes.WinDLL(name, winmode=0)  # type: ignore[attr-defined]


def missing_cuda_runtime_libraries(
    loader: Callable[[str], None] | None = None,
) -> list[str]:
    if os.name != "nt":
        return []
    load = loader or _load_library
    missing: list[str] = []
    for name in CUDA_RUNTIME_LIBRARIES:
        try:
            load(name)
        except OSError:
            missing.append(name)
    return missing


def check_cuda() -> CheckResult:
    details = detect_gpu_details()
    try:
        import ctranslate2

        details["ctranslate2_version"] = str(getattr(ctranslate2, "__version__", "installed"))
        count = int(ctranslate2.get_cuda_device_count())
        details["count"] = count
    except ImportError as exc:
        return CheckResult(
            "cuda",
            False,
            "GPU는 감지했지만 CTranslate2가 설치되어 있지 않습니다.",
            {**details, "error": str(exc)},
            status=MISSING,
            priority=40,
        )
    except (OSError, RuntimeError) as exc:
        return CheckResult(
            "cuda",
            False,
            "CTranslate2 CUDA 초기화에 실패했습니다.",
            {**details, "error": str(exc)},
            status=ERROR,
            priority=40,
        )
    if count < 1:
        return CheckResult(
            "cuda",
            False,
            "CUDA 장치를 찾을 수 없습니다.",
            details,
            status=BLOCKED,
            priority=40,
        )
    try:
        supported_compute_types = sorted(ctranslate2.get_supported_compute_types("cuda", 0))
    except (OSError, RuntimeError) as exc:
        return CheckResult(
            "cuda",
            False,
            "CUDA 장치는 감지했지만 CTranslate2 연산 초기화에 실패했습니다.",
            {**details, "error": str(exc)},
            status=ERROR,
            priority=40,
        )
    details["supported_compute_types"] = supported_compute_types
    if "float16" not in supported_compute_types:
        return CheckResult(
            "cuda",
            False,
            "CUDA 장치에서 float16 연산을 사용할 수 없습니다.",
            details,
            status=BLOCKED,
            priority=40,
        )
    missing_libraries = missing_cuda_runtime_libraries()
    if missing_libraries:
        details["missing_libraries"] = missing_libraries
        return CheckResult(
            "cuda",
            False,
            "CUDA 장치는 감지했지만 CUDA 12 런타임 라이브러리를 불러올 수 없습니다: "
            + ", ".join(missing_libraries)
            + ". NVIDIA CUDA 12 런타임을 설치하십시오.",
            details,
            status=MISSING,
            priority=40,
        )
    return CheckResult(
        "cuda",
        True,
        f"CTranslate2 CUDA 장치 {count}개에서 float16 연산을 사용할 수 있습니다.",
        details,
        priority=40,
    )


def check_cpu_runtime() -> CheckResult:
    details: dict[str, Any] = {"logical_cores": os.cpu_count() or 1}
    try:
        import ctranslate2

        details["ctranslate2_version"] = str(getattr(ctranslate2, "__version__", "installed"))
        supported_compute_types = sorted(ctranslate2.get_supported_compute_types("cpu"))
        details["supported_compute_types"] = supported_compute_types
    except ImportError as exc:
        return CheckResult(
            "cpu_runtime",
            False,
            "CPU 전사에 필요한 CTranslate2가 설치되어 있지 않습니다.",
            {**details, "error": str(exc)},
            status=MISSING,
            priority=45,
        )
    except (OSError, RuntimeError) as exc:
        return CheckResult(
            "cpu_runtime",
            False,
            "CTranslate2 CPU 연산 초기화에 실패했습니다.",
            {**details, "error": str(exc)},
            status=ERROR,
            priority=45,
        )
    if "int8" not in supported_compute_types:
        return CheckResult(
            "cpu_runtime",
            False,
            "이 CPU에서 CTranslate2 int8 연산을 사용할 수 없습니다.",
            details,
            status=BLOCKED,
            priority=45,
        )
    return CheckResult(
        "cpu_runtime",
        True,
        "CTranslate2 CPU int8 연산을 사용할 수 있습니다.",
        details,
        priority=45,
    )


def check_python_dependencies() -> CheckResult:
    versions: dict[str, str] = {}
    missing: list[str] = []
    for import_name, display_name in (
        ("av", "PyAV"),
        ("ctranslate2", "CTranslate2"),
        ("faster_whisper", "faster-whisper"),
        ("onnxruntime", "onnxruntime (Silero VAD)"),
    ):
        try:
            module = __import__(import_name)
            versions[display_name] = str(getattr(module, "__version__", "installed"))
        except ImportError:
            missing.append(display_name)
    details = {"python": sys.executable, "versions": versions, "missing": missing}
    if missing:
        return CheckResult(
            "python_dependencies",
            False,
            f"필수 Python 패키지가 없습니다: {', '.join(missing)}",
            details,
            status=MISSING,
            priority=30,
        )
    return CheckResult(
        "python_dependencies",
        True,
        "필수 Python 패키지를 사용할 수 있습니다.",
        details,
        priority=30,
    )


def codex_status() -> CheckResult:
    executable = resolve_executable("codex")
    if not executable:
        return CheckResult(
            "codex",
            False,
            "Codex CLI가 설치되어 있지 않습니다.",
            status=MISSING,
            priority=50,
            required=False,
        )
    details: dict[str, Any] = {"path": str(executable)}
    version = run_command(prepare_command(executable, "--version"), timeout=30)
    details["version"] = _first_output_line(version)
    if os.environ.get("OPENAI_API_KEY"):
        return CheckResult(
            "codex",
            False,
            "OPENAI_API_KEY가 설정되어 있어 API 과금 가능성이 있습니다.",
            details,
            status=BLOCKED,
            priority=50,
            required=False,
        )
    result = run_command(prepare_command(executable, "login", "status"), timeout=30)
    message = (result.stdout or result.stderr).strip()
    lowered = message.lower()
    ok = result.returncode == 0 and "logged in" in lowered and "chatgpt" in lowered
    details["login"] = message or "unknown"
    if ok:
        return CheckResult(
            "codex",
            True,
            "Codex ChatGPT 구독 로그인을 사용할 수 있습니다.",
            details,
            priority=50,
            required=False,
        )
    return CheckResult(
        "codex",
        False,
        "Codex가 설치되어 있지만 ChatGPT 구독 로그인이 필요합니다.",
        details,
        status=LOGIN_REQUIRED,
        priority=50,
        required=False,
    )


def _claude_oauth_expiry_ms() -> int | None:
    """Read only Claude Code's OAuth expiry timestamp, never its credentials."""

    config_root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        payload = json.loads((config_root / ".credentials.json").read_text(encoding="utf-8"))
        expiry = payload.get("claudeAiOauth", {}).get("expiresAt")
        return int(expiry) if expiry is not None else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def claude_status() -> CheckResult:
    executable = resolve_executable("claude")
    if not executable:
        return CheckResult(
            "claude",
            False,
            "Claude Code가 설치되어 있지 않습니다.",
            status=MISSING,
            priority=60,
            required=False,
        )
    details: dict[str, Any] = {"path": str(executable)}
    version = run_command(prepare_command(executable, "--version"), timeout=30)
    details["version"] = _first_output_line(version)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return CheckResult(
            "claude",
            False,
            "ANTHROPIC_API_KEY가 설정되어 있어 API 과금 가능성이 있습니다.",
            details,
            status=BLOCKED,
            priority=60,
            required=False,
        )
    result = run_command(prepare_command(executable, "auth", "status", "--json"), timeout=30)
    raw = (result.stdout or result.stderr).strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return CheckResult(
            "claude",
            False,
            "Claude가 설치되어 있지만 로그인 상태를 확인할 수 없습니다.",
            {**details, "raw_status": raw},
            status=LOGIN_REQUIRED,
            priority=60,
            required=False,
        )
    # Keep only what the app needs; the full payload includes account e-mail and org ids.
    details.update(
        {
            key: payload.get(key)
            for key in ("loggedIn", "authMethod", "subscriptionType")
            if key in payload
        }
    )
    ok = (
        result.returncode == 0
        and bool(payload.get("loggedIn"))
        and payload.get("authMethod") == "claude.ai"
    )
    auth_method = payload.get("authMethod", "unknown")
    subscription = payload.get("subscriptionType", "unknown")
    expiry_ms = _claude_oauth_expiry_ms()
    if expiry_ms is not None:
        details["credential_expired"] = expiry_ms <= int(time.time() * 1000)
    if ok and details.get("credential_expired"):
        return CheckResult(
            "claude",
            False,
            "Claude 구독 로그인 토큰이 만료되었습니다. 다시 로그인하십시오.",
            details,
            status=LOGIN_REQUIRED,
            priority=60,
            required=False,
        )
    if ok:
        message = (
            f"Claude 구독 로그인을 사용할 수 있습니다. 인증={auth_method}, 구독={subscription}"
        )
        status = READY
    else:
        message = "Claude가 설치되어 있지만 claude.ai 구독 로그인이 필요합니다."
        status = LOGIN_REQUIRED
    return CheckResult(
        "claude",
        ok,
        message,
        details,
        status=status,
        priority=60,
        required=False,
    )


def _safe_check(name: str, priority: int, check: Callable[[], CheckResult]) -> CheckResult:
    try:
        return check()
    except Exception as exc:
        return CheckResult(
            name,
            False,
            f"진단 중 오류가 발생했습니다: {exc}",
            {"error_type": type(exc).__name__, "error": str(exc)},
            status=ERROR,
            priority=priority,
            required=name not in {"codex", "claude"},
        )


def diagnose(video_path: Path | None = None) -> list[CheckResult]:
    checks: list[tuple[str, int, Callable[[], CheckResult]]] = []
    if video_path is not None:
        checks.append(("media", 10, lambda: probe_media(video_path)))
    checks.extend(
        [
            ("ffprobe", 20, check_ffprobe),
            ("python_dependencies", 30, check_python_dependencies),
            ("cuda", 40, check_cuda),
            ("codex", 50, codex_status),
            ("claude", 60, claude_status),
        ]
    )
    return [_safe_check(name, priority, check) for name, priority, check in checks]


def required_checks_ready(results: list[CheckResult], provider: str) -> bool:
    by_name = {item.name: item for item in results}
    essential_names = {"media", "ffprobe", "python_dependencies", "cuda"}
    if not essential_names.issubset(by_name):
        return False
    return all(by_name[name].ok for name in essential_names) and bool(
        by_name.get(provider) and by_name[provider].ok
    )


def first_blocker(results: list[CheckResult], provider: str) -> CheckResult | None:
    required_names = {"media", "ffprobe", "python_dependencies", "cuda", provider}
    candidates = [item for item in results if item.name in required_names and not item.ok]
    return min(candidates, key=lambda item: item.priority, default=None)
