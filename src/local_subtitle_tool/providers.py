from __future__ import annotations

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .codex_events import UNAVAILABLE, CodexRunState, parse_event
from .preflight import (
    CheckResult,
    UnsafeCommandArgument,
    claude_status,
    codex_status,
    prepare_command,
    resolve_executable,
    run_command,
)

PhaseCallback = Callable[[str], None]


@dataclass(slots=True)
class ProviderResult:
    payload: dict[str, Any]
    stdout: str
    stderr: str
    elapsed_seconds: float
    session_id: str | None = None
    session_mode: str = "ephemeral"
    metrics: dict[str, Any] = field(default_factory=dict)


class ProviderFailureKind(StrEnum):
    TRANSIENT_ERROR = "TRANSIENT_ERROR"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    USAGE_LIMIT = "USAGE_LIMIT"
    CONTENT_REFUSAL = "CONTENT_REFUSAL"
    UNKNOWN_PROVIDER_FAILURE = "UNKNOWN_PROVIDER_FAILURE"


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: ProviderFailureKind = ProviderFailureKind.UNKNOWN_PROVIDER_FAILURE,
        stdout: str = "",
        stderr: str = "",
        raw_response: str = "",
        returncode: int | None = None,
        metrics: dict[str, Any] | None = None,
        session_id: str | None = None,
        session_mode: str = "ephemeral",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.stdout = stdout
        self.stderr = stderr
        self.raw_response = raw_response
        self.returncode = returncode
        self.metrics = dict(metrics or {})
        self.session_id = session_id
        self.session_mode = session_mode


class ProviderCancelled(RuntimeError):
    pass


_USER_FAILURE_MESSAGES = {
    ProviderFailureKind.AUTH_REQUIRED: (
        "번역 계정 로그인이 필요합니다. 로그인 상태를 확인한 뒤 이전 작업에서 "
        "이어서 진행하십시오."
    ),
    ProviderFailureKind.USAGE_LIMIT: (
        "번역 모델 사용량 한도에 도달했습니다. 사용량이 회복된 뒤 이전 작업에서 "
        "이어서 진행하십시오."
    ),
    ProviderFailureKind.TRANSIENT_ERROR: (
        "번역 서비스에 일시적인 문제가 발생했습니다. 잠시 후 이전 작업에서 "
        "이어서 진행하십시오."
    ),
    ProviderFailureKind.INVALID_OUTPUT: (
        "번역 모델의 응답 형식이 올바르지 않습니다. 이전 작업에서 다시 시도하십시오."
    ),
    ProviderFailureKind.CONTENT_REFUSAL: (
        "번역 모델이 이 요청을 처리하지 않았습니다. 해당 배치를 검토하십시오."
    ),
    ProviderFailureKind.UNKNOWN_PROVIDER_FAILURE: (
        "번역 CLI 실행에 실패했습니다. 자세한 내용은 작업 기록에서 확인하십시오."
    ),
}
_DETAIL_MARKERS = (
    "error",
    "failed",
    "usage limit",
    "rate limit",
    "quota",
    "too many requests",
    "unauthorized",
    "authentication",
    "login",
    "429",
    "오류",
    "실패",
    "사용량 한도",
    "요청 한도",
    "로그인",
    "인증",
)
MAX_PROVIDER_USER_MESSAGE = 300
MAX_PROVIDER_DETAIL = 200


def _provider_error_detail(text: str) -> str:
    """Extract one diagnostic line without exposing the submitted prompt."""

    for raw_line in reversed(text.splitlines()[-80:]):
        line = " ".join(raw_line.split())
        if not line or len(line) > 500:
            continue
        normalized = line.casefold()
        if any(marker in normalized for marker in _DETAIL_MARKERS):
            if len(line) > MAX_PROVIDER_DETAIL:
                return line[: MAX_PROVIDER_DETAIL - 1].rstrip() + "…"
            return line
    return ""


def provider_user_message(kind: ProviderFailureKind, diagnostic_text: str = "") -> str:
    """Return a bounded UI/manifest message; raw CLI output stays on ProviderError."""

    message = _USER_FAILURE_MESSAGES[kind]
    detail = _provider_error_detail(diagnostic_text)
    if detail and detail.casefold() not in message.casefold():
        message = f"{message} 세부 정보: {detail}"
    if len(message) > MAX_PROVIDER_USER_MESSAGE:
        message = message[: MAX_PROVIDER_USER_MESSAGE - 1].rstrip() + "…"
    return message


_ACTIVE_PROCESSES: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()


def _register_process(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.add(process)


def _unregister_process(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_PROCESSES.discard(process)


def terminate_active_processes() -> int:
    """Kill every running translation CLI process tree (used on app shutdown)."""

    with _ACTIVE_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        try:
            _terminate_process_tree(process)
        except (OSError, subprocess.SubprocessError):
            continue
    return len(processes)


def classify_provider_failure(
    text: str,
    *,
    invalid_output: bool = False,
    timed_out: bool = False,
) -> ProviderFailureKind:
    """Classify provider failures conservatively from CLI output.

    Refusal detection intentionally requires an explicit refusal phrase. Generic
    safety-related words are not enough, because ordinary subtitle source text
    can contain them.
    """

    normalized = " ".join(text.casefold().split())
    auth_markers = (
        "not logged in",
        "login required",
        "authentication required",
        "authentication failed",
        "unauthorized",
        "invalid oauth",
        "로그인이 필요",
        "인증이 필요",
        "401 unauthorized",
    )
    usage_markers = (
        "usage limit",
        "rate limit",
        "quota exceeded",
        "credit balance",
        "too many requests",
        "사용량 한도",
        "요청 한도",
        "429 too many requests",
    )
    refusal_markers = (
        "i can't assist with",
        "i cannot assist with",
        "i can't help with",
        "i cannot help with",
        "i can't comply with",
        "i cannot comply with",
        "i'm unable to help with",
        "i am unable to help with",
        "i'm not able to help with",
        "i won't be able to help with",
        "i can't translate",
        "i cannot translate",
        "i must refuse",
        "i must decline",
        "도와드릴 수 없습니다",
        "도움을 드릴 수 없습니다",
        "요청을 처리할 수 없습니다",
        "요청에는 응할 수 없습니다",
        "요청에 응할 수 없습니다",
        "요청을 거절",
        "번역할 수 없습니다",
        "번역해 드릴 수 없습니다",
        "번역을 제공할 수 없습니다",
        "お手伝いできません",
        "対応できません",
        "翻訳できません",
        "お応えできません",
    )
    transient_markers = (
        "connection reset",
        "connection refused",
        "network error",
        "temporarily unavailable",
        "service unavailable",
        "internal server error",
        "gateway timeout",
        "timed out",
        "timeout",
        "연결이 재설정",
        "일시적으로 사용할 수 없",
    )
    if any(marker in normalized for marker in auth_markers):
        return ProviderFailureKind.AUTH_REQUIRED
    if any(marker in normalized for marker in usage_markers):
        return ProviderFailureKind.USAGE_LIMIT
    if any(marker in normalized for marker in refusal_markers):
        return ProviderFailureKind.CONTENT_REFUSAL
    if timed_out or any(marker in normalized for marker in transient_markers):
        return ProviderFailureKind.TRANSIENT_ERROR
    if invalid_output:
        return ProviderFailureKind.INVALID_OUTPUT
    return ProviderFailureKind.UNKNOWN_PROVIDER_FAILURE


def _read_output_text(output_path: Path) -> str:
    try:
        return output_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def find_codex_session_log(thread_id: str) -> Path | None:
    """Codex's own rollout log for a thread (only its modification time is read)."""

    sessions = _codex_home() / "sessions"
    try:
        matches = list(sessions.glob(f"*/*/*/rollout-*{thread_id}.jsonl"))
    except OSError:
        return None
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


class TranslationProvider:
    name: str
    # Providers that can keep one conversation across batches (see translator).
    supports_sessions: bool = False
    # Providers whose stdout is a JSONL event stream that can be parsed while running.
    streams_events: bool = False
    # Test hook: replaces the resolved CLI executable (e.g. [python, fake_cli.py]).
    executable_override: list[str] | None = None
    # Seconds without any stdout line before a streaming request counts as stalled.
    idle_timeout_seconds: float | None = None

    def status(self) -> CheckResult:
        raise NotImplementedError

    @staticmethod
    def _log_size(activity_log: Path) -> int | None:
        """Current size of Codex's session log.

        Only the size is used: on Windows the modification time of a file that Codex
        keeps open stays at its creation time (verified 2026-11-16), while the size
        grows with every reasoning event.
        """

        try:
            return activity_log.stat().st_size
        except OSError:
            return None

    def login_command(self) -> list[str]:
        raise NotImplementedError

    def build_command(
        self,
        *,
        model: str,
        reasoning_effort: str,
        speed_mode: str,
        schema_path: Path,
        output_path: Path,
        workdir: Path,
        session_id: str | None = None,
        persistent: bool = False,
    ) -> list[str]:
        raise NotImplementedError

    def parse_output(self, *, stdout: str, output_path: Path) -> dict[str, Any]:
        raise NotImplementedError

    def run(
        self,
        *,
        prompt: str,
        model: str,
        reasoning_effort: str = "default",
        speed_mode: str = "standard",
        schema_path: Path,
        output_path: Path,
        workdir: Path,
        timeout: int = 1800,
        cancel_event: threading.Event | None = None,
        session_id: str | None = None,
        persistent: bool = False,
        on_phase: PhaseCallback | None = None,
        idle_timeout: float | None = None,
    ) -> ProviderResult:
        if idle_timeout is None:
            idle_timeout = self.idle_timeout_seconds
        status = self.status()
        if not status.ok:
            kind = classify_provider_failure(status.message)
            raise ProviderError(
                provider_user_message(kind, status.message),
                kind=kind,
                stderr=status.message,
            )
        session_mode = (
            "resume"
            if session_id
            else ("new" if persistent and self.supports_sessions else "ephemeral")
        )
        try:
            command = self.build_command(
                model=model,
                reasoning_effort=reasoning_effort,
                speed_mode=speed_mode,
                schema_path=schema_path,
                output_path=output_path,
                workdir=workdir,
                **(
                    {"session_id": session_id, "persistent": persistent}
                    if self.supports_sessions
                    else {}
                ),
            )
        except UnsafeCommandArgument as exc:
            raise ProviderError(str(exc)) from exc
        output_path.unlink(missing_ok=True)
        state = CodexRunState()
        stdout_lines: list[str] = []
        started_at = time.monotonic()
        first_event_at: float | None = None
        last_line_at = started_at
        activity_log: Path | None = None
        next_log_lookup = started_at
        log_size: int | None = None
        log_growth_at = started_at

        def emit(phase: str | None) -> None:
            if phase and on_phase is not None:
                on_phase(phase)

        emit("세션 연결" if session_mode == "resume" else "번역 CLI 시작")
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        with tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace"
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=workdir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=False,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise ProviderError(
                    f"번역 CLI를 시작할 수 없습니다: {exc}",
                    kind=ProviderFailureKind.AUTH_REQUIRED
                    if isinstance(exc, FileNotFoundError)
                    else ProviderFailureKind.UNKNOWN_PROVIDER_FAILURE,
                ) from exc
            _register_process(process)
            lines: queue.Queue[str | None] = queue.Queue()

            def feed_stdin() -> None:
                try:
                    if process.stdin is not None:
                        process.stdin.write(prompt)
                        process.stdin.close()
                except OSError:
                    # The CLI exited before reading stdin; classified below.
                    pass

            def read_stdout() -> None:
                try:
                    if process.stdout is not None:
                        for line in process.stdout:
                            lines.put(line)
                except (OSError, ValueError):
                    pass
                finally:
                    lines.put(None)

            # stdin and stdout are serviced on their own threads and stderr goes to a
            # file, so a full pipe can never deadlock the CLI.
            writer = threading.Thread(target=feed_stdin, daemon=True)
            reader = threading.Thread(target=read_stdout, daemon=True)
            writer.start()
            reader.start()
            try:
                stream_done = False
                while not stream_done:
                    if cancel_event and cancel_event.is_set():
                        _terminate_process_tree(process)
                        raise ProviderCancelled("사용자 요청으로 번역을 중지했습니다.")
                    if time.monotonic() - started_at > timeout:
                        _terminate_process_tree(process)
                        message = f"번역 CLI가 제한 시간 {timeout}초를 초과했습니다."
                        raise ProviderError(
                            message,
                            kind=classify_provider_failure(message, timed_out=True),
                        )
                    if idle_timeout and activity_log is not None:
                        size = self._log_size(activity_log)
                        if size is not None and size != log_size:
                            log_size = size
                            log_growth_at = time.monotonic()
                    # `codex exec --json` prints nothing while reasoning, so a request is
                    # stalled only when neither stdout nor Codex's session log has grown.
                    # Without the log there is no reliable signal (overall timeout only).
                    if (
                        idle_timeout
                        and activity_log is not None
                        and time.monotonic() - max(last_line_at, log_growth_at) > idle_timeout
                    ):
                        _terminate_process_tree(process)
                        raise ProviderError(
                            f"번역 CLI가 {int(idle_timeout)}초 동안 응답하지 않아 "
                            "요청을 중단했습니다. 새 세션으로 다시 보냅니다.",
                            kind=ProviderFailureKind.TRANSIENT_ERROR,
                            stdout="".join(stdout_lines),
                            session_id=state.thread_id,
                            session_mode=session_mode,
                        )
                    if (
                        idle_timeout
                        and activity_log is None
                        and state.thread_id
                        and time.monotonic() >= next_log_lookup
                    ):
                        activity_log = find_codex_session_log(state.thread_id)
                        next_log_lookup = time.monotonic() + 5
                    try:
                        line = lines.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if line is None:
                        stream_done = True
                        continue
                    last_line_at = time.monotonic()
                    stdout_lines.append(line)
                    if first_event_at is None:
                        first_event_at = time.monotonic()
                    if self.streams_events:
                        event = parse_event(line)
                        if event is not None:
                            emit(state.apply(event))
                while process.poll() is None:
                    if cancel_event and cancel_event.is_set():
                        _terminate_process_tree(process)
                        raise ProviderCancelled("사용자 요청으로 번역을 중지했습니다.")
                    time.sleep(0.05)
            finally:
                _unregister_process(process)
                reader.join(timeout=2)
                writer.join(timeout=2)
            stderr_file.seek(0)
            stderr = stderr_file.read()
            returncode = int(process.returncode or 0)
        stdout = "".join(stdout_lines)
        finished_at = time.monotonic()
        raw_text = _read_output_text(output_path)
        metrics: dict[str, Any] = {
            "request_chars": len(prompt),
            "response_chars": len(raw_text) or state.agent_message_chars or None,
            "time_to_first_event_seconds": (
                round(first_event_at - started_at, 3) if first_event_at is not None else None
            ),
            "total_elapsed_seconds": round(finished_at - started_at, 3),
            "session_mode": session_mode,
            "requested_service_tier": "fast" if speed_mode == "fast" else "default",
        }
        if self.streams_events:
            metrics.update(state.metrics())
        else:
            metrics.update(
                {
                    "session_id": None,
                    "observed_service_tier": UNAVAILABLE,
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "cache_write_input_tokens": None,
                    "output_tokens": None,
                    "reasoning_output_tokens": None,
                }
            )
        observed_session = state.thread_id or session_id
        failure_text = "\n".join((*state.error_messages, stderr))
        if returncode != 0 or state.error_messages and not state.turn_completed:
            combined = "\n".join((failure_text, stdout, raw_text))
            kind = classify_provider_failure(combined)
            raise ProviderError(
                provider_user_message(kind, failure_text),
                kind=kind,
                stdout=stdout,
                stderr=stderr,
                raw_response=raw_text,
                returncode=returncode,
                metrics=metrics,
                session_id=observed_session,
                session_mode=session_mode,
            )
        emit("구조 검증 중")
        try:
            payload = self.parse_output(stdout=stdout, output_path=output_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raw_response = raw_text or stdout
            message = f"번역 CLI의 구조화 결과를 해석할 수 없습니다: {exc}"
            combined = "\n".join((message, raw_response, stderr))
            raise ProviderError(
                message,
                kind=classify_provider_failure(combined, invalid_output=True),
                stdout=stdout,
                stderr=stderr,
                raw_response=raw_response,
                returncode=returncode,
                metrics=metrics,
                session_id=observed_session,
                session_mode=session_mode,
            ) from exc
        return ProviderResult(
            payload=payload,
            stdout=stdout,
            stderr=stderr,
            elapsed_seconds=finished_at - started_at,
            session_id=observed_session if self.supports_sessions else None,
            session_mode=session_mode,
            metrics=metrics,
        )

    def launch_login(self) -> None:
        command = self.login_command()
        executable = resolve_executable(command[0])
        if not executable:
            raise ProviderError(f"{command[0]} 실행 파일을 찾을 수 없습니다.")
        invocation = prepare_command(executable, *command[1:])
        flags = subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
        subprocess.Popen(invocation, creationflags=flags, close_fds=True, shell=False)

    def logout_command(self) -> list[str]:
        raise NotImplementedError

    def logout(self) -> None:
        """Sign out through the official CLI; the CLI removes its own stored credentials."""

        command = self.logout_command()
        executable = resolve_executable(command[0])
        if not executable:
            raise ProviderError(f"{command[0]} 실행 파일을 찾을 수 없습니다.")
        result = run_command(prepare_command(executable, *command[1:]), timeout=60)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ProviderError(f"{self.name} 로그아웃에 실패했습니다: {detail}")


class CodexProvider(TranslationProvider):
    name = "codex"
    supports_sessions = True
    streams_events = True
    # While reasoning, Codex's session log grows every ~6 s; while it writes the final
    # answer the log is silent (90 s for a 240-line batch on 2026-11-16). 420 s leaves
    # room for that and still ends a truly stuck request far sooner than the 30-minute
    # overall timeout.
    idle_timeout_seconds = 420.0

    def status(self) -> CheckResult:
        return codex_status()

    def login_command(self) -> list[str]:
        return ["codex", "login"]

    def logout_command(self) -> list[str]:
        return ["codex", "logout"]

    def build_command(
        self,
        *,
        model: str,
        reasoning_effort: str,
        speed_mode: str,
        schema_path: Path,
        output_path: Path,
        workdir: Path,
        session_id: str | None = None,
        persistent: bool = False,
    ) -> list[str]:
        """`codex exec` for a new (ephemeral or persistent) session, or `exec resume`.

        `exec resume` has no --sandbox flag, so read-only sandboxing is passed as
        config; verified to stay read-only on resumed turns (codex-cli 0.167).
        """

        del workdir
        if session_id:
            options = [
                "exec",
                "resume",
                "--json",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "-c",
                'sandbox_mode="read-only"',
            ]
        else:
            options = ["exec", "--json"]
            if not persistent:
                options.append("--ephemeral")
            options.extend(
                (
                    "--ignore-user-config",
                    "--ignore-rules",
                    "--sandbox",
                    "read-only",
                    "--skip-git-repo-check",
                )
            )
        if reasoning_effort != "default":
            options.extend(("-c", f'model_reasoning_effort="{reasoning_effort}"'))
        if speed_mode == "fast":
            options.extend(("-c", 'service_tier="fast"', "--enable", "fast_mode"))
        options.extend(
            (
                "--model",
                model,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
            )
        )
        if session_id:
            options.append(session_id)
        options.append("-")
        if self.executable_override:
            return [*self.executable_override, *options]
        executable = resolve_executable("codex") or Path("codex")
        return prepare_command(executable, *options)

    def parse_output(self, *, stdout: str, output_path: Path) -> dict[str, Any]:
        del stdout
        return json.loads(output_path.read_text(encoding="utf-8"))


class ClaudeProvider(TranslationProvider):
    name = "claude"

    def status(self) -> CheckResult:
        return claude_status()

    def logout_command(self) -> list[str]:
        return ["claude", "auth", "logout"]

    def login_command(self) -> list[str]:
        return ["claude", "auth", "login"]

    def build_command(
        self,
        *,
        model: str,
        reasoning_effort: str,
        speed_mode: str,
        schema_path: Path,
        output_path: Path,
        workdir: Path,
        session_id: str | None = None,
        persistent: bool = False,
    ) -> list[str]:
        del output_path, workdir, session_id, persistent
        if speed_mode != "standard":
            raise ProviderError(
                "Claude CLI는 Fast 모드를 지원하지 않습니다. Standard를 선택하십시오."
            )
        executable = resolve_executable("claude") or Path("claude")
        # One line: a multi-line argument is cut at the first newline by cmd.exe when
        # Claude Code is installed through npm (claude.cmd).
        schema = json.dumps(
            json.loads(schema_path.read_text(encoding="utf-8")),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        options = [
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--model",
            model,
        ]
        if reasoning_effort != "default":
            options.extend(("--effort", reasoning_effort))
        options.extend(
            (
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--no-session-persistence",
            )
        )
        return prepare_command(
            executable,
            *options,
        )

    def parse_output(self, *, stdout: str, output_path: Path) -> dict[str, Any]:
        del output_path
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict):
            raise ValueError("Claude 응답 envelope이 객체가 아닙니다.")
        if envelope.get("is_error"):
            detail = str(envelope.get("result") or envelope.get("subtype") or "")
            raise ValueError(f"Claude가 오류 응답을 반환했습니다: {detail}")
        if isinstance(envelope.get("structured_output"), dict):
            return envelope["structured_output"]
        result = envelope.get("result")
        if isinstance(result, str):
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
        if "items" in envelope:
            return envelope
        raise ValueError("Claude 응답에 structured_output이 없습니다.")


def get_provider(name: str) -> TranslationProvider:
    if name == "codex":
        return CodexProvider()
    if name == "claude":
        return ClaudeProvider()
    raise ValueError(f"지원하지 않는 번역 제공자입니다: {name}")
