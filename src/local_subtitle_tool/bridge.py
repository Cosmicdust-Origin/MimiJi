from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    Property,
    QCoreApplication,
    QObject,
    QSettings,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog

from .accounts import account_line, summarize_account
from .app_logging import get_logger
from .appinfo import APP_ID, APP_NAME, APP_VERSION, LEGACY_APP_IDS
from .core import JobConfig, JobManifest, JobPaths, infer_title, load_json, safe_file_stem
from .diagnostics import (
    DiagnosticReport,
    load_diagnostic_report,
    run_full_diagnostic,
)
from .diarization import (
    download_diarization_model,
    inspect_diarization_status,
)
from .i18n import UI_LANGUAGES
from .i18n import translate as translate_ui
from .installers import (
    install_claude,
    install_codex,
    install_diarization_dependencies,
    install_ffmpeg,
    install_python_dependencies,
    is_frozen,
    login_huggingface,
    open_official_page,
)
from .jobs import JobRecord, JobRegistry
from .languages import DIRECTIONS, direction
from .model_manager import default_model_cache_dir, download_model, inspect_model_cache
from .providers import (
    ProviderError,
    ProviderFailureKind,
    get_provider,
    terminate_active_processes,
)
from .subtitles import ValidationResult, build_and_validate
from .transcriber import TranscriptionCancelled, transcribe
from .translator import (
    TranslationCancelled,
    TranslationPartialError,
    TranslationReviewRequired,
    UnverifiableTranslationBatch,
    accept_model_translations,
    omit_review_items,
    pending_review_items,
    retry_review_item,
    save_manual_translation,
    translate,
)

_logger = get_logger("bridge")
# Values that are content (file names, paths, transcript text, model notes), not UI text.
_UNTRANSLATED_KEYS = frozenset(
    {
        "raw_ja", "ja_fixed", "ko", "path", "file_name", "video_path", "output_dir",
        "code", "job_id", "reason", "time", "size", "url", "provider", "id", "raw_id",
        "updated_text", "elapsed_text", "kind",
    }
)

REASONING_OPTIONS = {
    "codex": ["default", "low", "medium", "high", "xhigh"],
    "claude": ["default", "low", "medium", "high", "max"],
}
# Measured on 테스트 영상 A (2026-11-15): high was ~20% faster than xhigh but mistranslated
# more and left 8 review items instead of 2, so xhigh is the Codex default.
RECOMMENDED_REASONING = {"codex": "xhigh"}
DEFAULT_REASONING = {"codex": "xhigh", "claude": "high"}
# Luna is cheap per token, so the fast tier is the recommended Codex default.
RECOMMENDED_MODEL = {"codex": "gpt-5.6-luna"}
RECOMMENDED_SPEED = {"codex": "fast"}
DEFAULT_SPEED = {"codex": "fast", "claude": "standard"}
SPEED_OPTIONS = ["standard", "fast"]


def _recommended_labels(options: list[str], recommended: str | None) -> list[str]:
    return [f"{option} (추천)" if option == recommended else option for option in options]
SHUTDOWN_JOIN_SECONDS = 15.0
EXIT_JOIN_SECONDS = 5.0


def _format_duration(seconds: float | int | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


def _bounded_ui_message(error: object, limit: int = 300) -> str:
    message = " ".join(str(error).split())
    if len(message) <= limit:
        return message
    return message[: limit - 1].rstrip() + "…"


def _translation_block_kind(config: JobConfig) -> str:
    manifest = JobManifest.load_or_create(config)
    kind = str(manifest.stats.get("translation_blocked_kind", ""))
    if kind:
        return kind
    path = JobPaths(config).provider_events
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            event = json.loads(line)
            if event.get("outcome") == "failure" and event.get("failure_kind"):
                return str(event["failure_kind"])
    except (OSError, ValueError):
        return ""
    return ""


class UserActionRequired(RuntimeError):
    pass


def _default_settings() -> QSettings:
    """MimiJi settings, seeded once from the newest earlier name that has settings."""

    settings = QSettings(APP_ID, APP_ID)
    if not settings.allKeys():
        for legacy_id in LEGACY_APP_IDS:
            legacy = QSettings(legacy_id, legacy_id)
            keys = legacy.allKeys()
            if not keys:
                continue
            for key in keys:
                settings.setValue(key, legacy.value(key))
            settings.sync()
            break
    return settings


class AppBridge(QObject):
    stateChanged = Signal()
    formChanged = Signal()
    jobsChanged = Signal()
    diagnosticsChanged = Signal()
    logsChanged = Signal()
    resultsChanged = Signal()
    reviewChanged = Signal()
    accountsChanged = Signal()
    themeChanged = Signal()
    languageChanged = Signal()
    notification = Signal(str)

    _updateRequested = Signal(dict)
    _logRequested = Signal(str)
    _operationFinished = Signal(str, object, object)

    def __init__(
        self,
        *,
        settings: QSettings | None = None,
        registry: JobRegistry | None = None,
        auto_diagnose: bool = True,
    ) -> None:
        super().__init__()
        self._settings = settings or _default_settings()
        self._registry = registry or JobRegistry()
        self._auto_diagnose = auto_diagnose
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._workers: list[threading.Thread] = []
        self._quit_requested = False
        self._review_items: list[dict[str, Any]] = []
        self._review_busy = False
        self._review_config: JobConfig | None = None
        self._resume_choice_visible = False
        self._resume_settings_mode = False
        self._resume_original_config: JobConfig | None = None
        self._resume_choice_blocked = False
        self._unverified_batch_indexes: list[int] = []
        self._unverified_config: JobConfig | None = None
        self._accounts: dict[str, dict[str, Any]] = {
            name: summarize_account(name, None) for name in ("codex", "claude")
        }
        self._accounts_busy = False
        self._accounts_checked_at = 0.0

        self._screen = "diagnostic" if load_diagnostic_report() is None else "home"
        self._busy = False
        self._diagnostic_busy = False
        self._video_path = ""
        self._code = ""
        self._term_hints = ""
        self._output_dir = ""
        # True while the output folder is the automatic <video folder>/<title>.
        self._output_dir_auto = True
        self._file_meta = "영상을 선택하지 않았습니다."
        self._provider = str(self._settings.value("translation/provider", "codex"))
        self._model = str(
            self._settings.value(
                "translation/model",
                "gpt-5.6-luna" if self._provider == "codex" else "sonnet",
            )
        )
        default_reasoning = DEFAULT_REASONING.get(self._provider, "high")
        self._reasoning = str(self._settings.value("translation/reasoning", default_reasoning))
        if self._reasoning not in REASONING_OPTIONS.get(self._provider, []):
            self._reasoning = default_reasoning
        self._speed_mode = str(
            self._settings.value("translation/speed", DEFAULT_SPEED.get(self._provider, "standard"))
        )
        self._accelerator = str(self._settings.value("transcription/accelerator", "auto"))
        self._cpu_threads = int(self._settings.value("transcription/cpu_threads", 0) or 0)
        self._diarization_enabled = bool(
            self._settings.value("diarization/enabled", False, type=bool)
        )
        self._diarization_speakers = int(
            self._settings.value("diarization/expected_speakers", 0) or 0
        )
        self._diarization_alignment = str(
            self._settings.value("diarization/alignment", "basic")
        )
        self._diarization_device = str(
            self._settings.value("diarization/device", "auto")
        )
        self._diarization_srt_labels = bool(
            self._settings.value("diarization/srt_labels", False, type=bool)
        )
        self._diarization_busy = False
        self._dark_mode = bool(self._settings.value("ui/dark_mode", True, type=bool))
        self._ui_language = str(self._settings.value("ui/language", "ko"))
        if self._ui_language not in UI_LANGUAGES:
            self._ui_language = "ko"
        self._codex_sessions = bool(
            self._settings.value("translation/codex_sessions", True, type=bool)
        )
        self._language_pair = str(self._settings.value("translation/language_pair", "ja-ko"))
        if self._language_pair not in DIRECTIONS:
            self._language_pair = "ja-ko"
        self._parallel_batches = max(
            1, min(int(self._settings.value("translation/parallel_batches", 3, type=int)), 4)
        )
        self._request_phase = ""
        self._request_started: float | None = None
        self._first_event_seconds: float | None = None
        self._request_elapsed_text = ""
        diarization_status = inspect_diarization_status()
        self._diarization_available = not (
            is_frozen() and not diarization_status.runtime_ready
        )
        if not self._diarization_available:
            self._diarization_enabled = False
        self._diarization_ready = diarization_status.ready
        self._diarization_status = diarization_status.message

        self._task_title = "대기 중"
        self._task_detail = "영상과 번역 설정을 확인한 뒤 작업을 시작합니다."
        self._phase_index = 0
        self._overall_progress = 0.0
        self._phase_progress = 0.0
        self._session_started: float | None = None
        self._elapsed_text = "00:00:00"
        self._eta_text = "계산 전"
        self._last_saved_text = "—"
        self._last_error = ""
        self._system_summary = "환경 확인 필요"
        self._diagnostic_summary = "이 컴퓨터의 실행 환경을 확인하십시오."
        self._diagnostic_rows: list[dict[str, Any]] = []
        self._model_download_size = ""
        self._model_download_cache_path = ""
        self._model_download_free_space = ""
        self._refresh_model_download_info()
        self._jobs: list[dict[str, Any]] = []
        self._logs: list[dict[str, str]] = []
        self._result_files: list[dict[str, Any]] = []
        self._result_metrics: list[dict[str, str]] = []
        self._active_config: JobConfig | None = None

        self._updateRequested.connect(self._apply_update)
        self._logRequested.connect(self._append_log)
        self._operationFinished.connect(self._finish_operation)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @Property(str, notify=stateChanged)
    def screen(self) -> str:
        return self._screen

    @Property(bool, notify=stateChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=stateChanged)
    def diagnosticBusy(self) -> bool:
        return self._diagnostic_busy

    @Property(str, notify=formChanged)
    def videoPath(self) -> str:
        return self._video_path

    @videoPath.setter
    def videoPath(self, value: str) -> None:
        self._set_video_path(value)

    @Property(str, notify=formChanged)
    def code(self) -> str:
        return self._code

    @code.setter
    def code(self, value: str) -> None:
        normalized = " ".join(value.split())
        if normalized != self._code:
            self._code = normalized
            self._refresh_auto_output_dir()
            self.formChanged.emit()

    def _refresh_auto_output_dir(self) -> None:
        video = Path(self._video_path) if self._video_path else None
        if self._output_dir_auto and video is not None and video.is_file():
            folder = safe_file_stem(self._code) if self._code else video.stem
            self._output_dir = str((video.parent / folder).resolve())

    @Property(str, notify=formChanged)
    def outputDir(self) -> str:
        return self._output_dir

    @outputDir.setter
    def outputDir(self, value: str) -> None:
        self._output_dir_auto = not value.strip()
        normalized = value.strip()
        if normalized != self._output_dir:
            self._output_dir = normalized
            self.formChanged.emit()

    @Property(str, notify=formChanged)
    def fileName(self) -> str:
        return Path(self._video_path).name if self._video_path else "영상이 선택되지 않았습니다."

    @Property(str, notify=formChanged)
    def fileMeta(self) -> str:
        return self._t(self._raw_fileMeta())

    def _raw_fileMeta(self) -> str:
        return self._file_meta

    @Property(str, notify=formChanged)
    def provider(self) -> str:
        return self._provider

    @provider.setter
    def provider(self, value: str) -> None:
        normalized = value.strip().lower()
        if normalized not in {"codex", "claude"} or normalized == self._provider:
            return
        previous_default = self._model in {"gpt-5.6-luna", "sonnet"}
        self._provider = normalized
        if previous_default:
            self._model = "gpt-5.6-luna" if normalized == "codex" else "sonnet"
        self._speed_mode = DEFAULT_SPEED[normalized]
        if self._reasoning not in REASONING_OPTIONS[normalized]:
            self._reasoning = DEFAULT_REASONING[normalized]
        self.formChanged.emit()
        self.accountsChanged.emit()

    @Property(str, notify=formChanged)
    def providerLabel(self) -> str:
        return "Codex · ChatGPT" if self._provider == "codex" else "Claude Code"

    @Property(str, notify=formChanged)
    def model(self) -> str:
        return self._model

    @model.setter
    def model(self, value: str) -> None:
        normalized = value.strip()
        if normalized and normalized != self._model:
            self._model = normalized
            self.formChanged.emit()

    @Property(list, notify=formChanged)
    def modelOptions(self) -> list[str]:
        if self._provider == "codex":
            return ["gpt-5.6-luna", "gpt-6-luna"]
        return ["sonnet", "opus"]

    @Property(list, notify=formChanged)
    def reasoningOptions(self) -> list[str]:
        return list(REASONING_OPTIONS[self._provider])

    @Property(list, notify=formChanged)
    def reasoningLabels(self) -> list[str]:
        return self._t_list(self._raw_reasoningLabels())

    def _raw_reasoningLabels(self) -> list[str]:
        """Display labels for reasoningOptions (same order), marking the recommended one."""

        return _recommended_labels(
            list(REASONING_OPTIONS[self._provider]), RECOMMENDED_REASONING.get(self._provider)
        )

    @Property(list, notify=formChanged)
    def modelLabels(self) -> list[str]:
        return self._t_list(self._raw_modelLabels())

    def _raw_modelLabels(self) -> list[str]:
        return _recommended_labels(self.modelOptions, RECOMMENDED_MODEL.get(self._provider))

    @Property(list, notify=formChanged)
    def speedOptions(self) -> list[str]:
        return list(SPEED_OPTIONS)

    # --- screen language ------------------------------------------------------------------

    @Property(str, notify=languageChanged)
    def uiLanguage(self) -> str:
        return self._ui_language

    @uiLanguage.setter
    def uiLanguage(self, value: str) -> None:
        if value not in UI_LANGUAGES or value == self._ui_language:
            return
        self._ui_language = value
        self._settings.setValue("ui/language", value)
        self._settings.sync()
        self.languageChanged.emit()
        for signal in (
            self.stateChanged,
            self.formChanged,
            self.jobsChanged,
            self.diagnosticsChanged,
            self.logsChanged,
            self.resultsChanged,
            self.reviewChanged,
            self.accountsChanged,
        ):
            signal.emit()

    @Slot(str, str, result=str)
    def tr(self, text: str, language: str) -> str:
        return translate_ui(text, language)

    def _t(self, text: Any) -> Any:
        return translate_ui(text, self._ui_language) if isinstance(text, str) else text

    def _t_list(self, values: list[Any]) -> list[Any]:
        if self._ui_language == "ko":
            return values
        translated: list[Any] = []
        for value in values:
            if isinstance(value, dict):
                translated.append(
                    {
                        key: item if key in _UNTRANSLATED_KEYS else self._t(item)
                        for key, item in value.items()
                    }
                )
            else:
                translated.append(self._t(value))
        return translated

    def _notify(self, text: str) -> None:
        self.notification.emit(self._t(text))

    @Property(str, notify=formChanged)
    def termHints(self) -> str:
        return self._term_hints

    @termHints.setter
    def termHints(self, value: str) -> None:
        if value != self._term_hints:
            self._term_hints = value
            self.formChanged.emit()

    @Property(str, notify=formChanged)
    def languagePair(self) -> str:
        return self._language_pair

    @languagePair.setter
    def languagePair(self, value: str) -> None:
        if value in DIRECTIONS and value != self._language_pair:
            self._language_pair = value
            self._settings.setValue("translation/language_pair", value)
            self.formChanged.emit()

    @Property(list, constant=True)
    def languagePairOptions(self) -> list[str]:
        return list(DIRECTIONS)

    @Property(list, notify=languageChanged)
    def languagePairLabels(self) -> list[str]:
        return self._t_list(self._raw_languagePairLabels())

    def _raw_languagePairLabels(self) -> list[str]:
        return [item.label for item in DIRECTIONS.values()]

    def _shown_direction(self):
        config = self._review_config or self._active_config
        return config.direction if config is not None else direction(self._language_pair)

    @Property(str, notify=reviewChanged)
    def sourceLanguageName(self) -> str:
        return self._t(self._raw_sourceLanguageName())

    def _raw_sourceLanguageName(self) -> str:
        return self._shown_direction().source_name

    @Property(str, notify=reviewChanged)
    def targetLanguageName(self) -> str:
        return self._t(self._raw_targetLanguageName())

    def _raw_targetLanguageName(self) -> str:
        return self._shown_direction().target_name

    @Property(list, notify=formChanged)
    def speedLabels(self) -> list[str]:
        return self._t_list(self._raw_speedLabels())

    def _raw_speedLabels(self) -> list[str]:
        return _recommended_labels(list(SPEED_OPTIONS), RECOMMENDED_SPEED.get(self._provider))

    @Property(bool, constant=True)
    def canInstallPackages(self) -> bool:
        return not is_frozen()

    @Property(bool, notify=formChanged)
    def diarizationAvailable(self) -> bool:
        return self._diarization_available

    @Property(str, notify=diagnosticsChanged)
    def modelDownloadSize(self) -> str:
        return self._t(self._raw_modelDownloadSize())

    def _raw_modelDownloadSize(self) -> str:
        return self._model_download_size

    @Property(str, notify=diagnosticsChanged)
    def modelDownloadCachePath(self) -> str:
        return self._model_download_cache_path

    @Property(str, notify=diagnosticsChanged)
    def modelDownloadFreeSpace(self) -> str:
        return self._model_download_free_space

    @Property(bool, notify=formChanged)
    def codexSessions(self) -> bool:
        return self._codex_sessions

    @codexSessions.setter
    def codexSessions(self, value: bool) -> None:
        normalized = bool(value)
        if normalized == self._codex_sessions:
            return
        self._codex_sessions = normalized
        self._settings.setValue("translation/codex_sessions", normalized)
        self._settings.sync()
        self.formChanged.emit()

    @Property(str, notify=stateChanged)
    def requestPhase(self) -> str:
        return self._t(self._raw_requestPhase())

    def _raw_requestPhase(self) -> str:
        return self._request_phase

    @Property(str, notify=stateChanged)
    def requestElapsedText(self) -> str:
        return self._t(self._raw_requestElapsedText())

    def _raw_requestElapsedText(self) -> str:
        return self._request_elapsed_text

    @Property(str, notify=stateChanged)
    def firstEventText(self) -> str:
        return self._t(self._raw_firstEventText())

    def _raw_firstEventText(self) -> str:
        if self._first_event_seconds is None:
            return "대기 중" if self._request_started is not None else "—"
        return f"{self._first_event_seconds:.1f}초"

    def _phase_from_worker(self, text: str) -> None:
        self._updateRequested.emit({"request_phase_event": text})

    @Property(bool, notify=themeChanged)
    def darkMode(self) -> bool:
        return self._dark_mode

    @darkMode.setter
    def darkMode(self, value: bool) -> None:
        normalized = bool(value)
        if normalized == self._dark_mode:
            return
        self._dark_mode = normalized
        # A display preference: saved immediately, independent of "설정 저장".
        self._settings.setValue("ui/dark_mode", normalized)
        self._settings.sync()
        self.themeChanged.emit()

    @Property(str, constant=True)
    def appName(self) -> str:
        return APP_NAME

    @Property(str, constant=True)
    def appVersion(self) -> str:
        return APP_VERSION

    @Property(list, notify=accountsChanged)
    def accounts(self) -> list[dict[str, Any]]:
        return self._t_list(self._raw_accounts())

    def _raw_accounts(self) -> list[dict[str, Any]]:
        return [self._accounts["codex"], self._accounts["claude"]]

    @Property(bool, notify=accountsChanged)
    def accountsBusy(self) -> bool:
        return self._accounts_busy

    @Property(str, notify=accountsChanged)
    def providerAccountText(self) -> str:
        return self._t(self._raw_providerAccountText())

    def _raw_providerAccountText(self) -> str:
        return account_line(self._accounts[self._provider])

    @Property(bool, notify=accountsChanged)
    def providerAccountReady(self) -> bool:
        return bool(self._accounts[self._provider].get("logged_in"))

    @Slot()
    def refreshAccounts(self) -> None:
        """Ask the official CLIs for login status in the background."""

        if self._accounts_busy or self._busy:
            return
        self._accounts_busy = True
        self.accountsChanged.emit()
        self._launch(
            "accounts",
            lambda: {name: get_provider(name).status() for name in ("codex", "claude")},
        )

    @Slot()
    def refreshAccountsIfStale(self) -> None:
        """Refresh when the window regains focus (e.g. after a login console closes)."""

        if time.monotonic() - self._accounts_checked_at > 5.0:
            self.refreshAccounts()

    @Slot(str)
    def loginProvider(self, provider: str) -> None:
        if self._busy or provider not in self._accounts:
            return
        try:
            get_provider(provider).launch_login()
        except Exception as exc:
            self._notify(str(exc))
            return
        self._notify(
            "공식 로그인 창을 열었습니다. 로그인을 마치고 앱으로 돌아오면 상태를 다시 확인합니다."
        )

    @Slot(str)
    def logoutProvider(self, provider: str) -> None:
        if self._busy or self._accounts_busy or provider not in self._accounts:
            return
        self._accounts_busy = True
        self.accountsChanged.emit()

        def run() -> dict[str, Any]:
            get_provider(provider).logout()
            return {name: get_provider(name).status() for name in ("codex", "claude")}

        self._launch("account_logout", run)

    def _apply_account_checks(self, checks: dict[str, Any]) -> None:
        for name, check in checks.items():
            if name in self._accounts:
                self._accounts[name] = summarize_account(name, check)
        self._accounts_checked_at = time.monotonic()

    @Property(list, notify=reviewChanged)
    def reviewItems(self) -> list[dict[str, Any]]:
        return self._t_list(self._raw_reviewItems())

    def _raw_reviewItems(self) -> list[dict[str, Any]]:
        return self._review_items

    @Property(bool, notify=reviewChanged)
    def reviewBusy(self) -> bool:
        return self._review_busy

    @Property(int, notify=reviewChanged)
    def reviewUnresolvedCount(self) -> int:
        return sum(1 for item in self._review_items if not item.get("resolved"))

    @Property(str, notify=formChanged)
    def reasoning(self) -> str:
        return self._reasoning

    @reasoning.setter
    def reasoning(self, value: str) -> None:
        normalized = value.strip().lower()
        if normalized not in REASONING_OPTIONS[self._provider]:
            return
        if normalized != self._reasoning:
            self._reasoning = normalized
            self.formChanged.emit()

    @Property(str, notify=formChanged)
    def speedMode(self) -> str:
        return self._speed_mode

    @speedMode.setter
    def speedMode(self, value: str) -> None:
        normalized = value.strip().lower()
        if self._provider == "claude":
            normalized = "standard"
        if normalized != self._speed_mode:
            self._speed_mode = normalized
            self.formChanged.emit()

    @Property(str, notify=formChanged)
    def accelerator(self) -> str:
        return self._accelerator

    @accelerator.setter
    def accelerator(self, value: str) -> None:
        normalized = value.strip().lower()
        if normalized != self._accelerator:
            self._accelerator = normalized
            self.formChanged.emit()

    @Property(int, notify=formChanged)
    def cpuThreads(self) -> int:
        return self._cpu_threads

    @cpuThreads.setter
    def cpuThreads(self, value: int) -> None:
        normalized = max(0, int(value))
        if normalized != self._cpu_threads:
            self._cpu_threads = normalized
            self.formChanged.emit()

    @Property(bool, notify=formChanged)
    def diarizationEnabled(self) -> bool:
        return self._diarization_enabled

    @diarizationEnabled.setter
    def diarizationEnabled(self, value: bool) -> None:
        normalized = bool(value) and self._diarization_available
        if normalized != self._diarization_enabled:
            self._diarization_enabled = normalized
            self.formChanged.emit()

    @Property(int, notify=formChanged)
    def diarizationSpeakers(self) -> int:
        return self._diarization_speakers

    @diarizationSpeakers.setter
    def diarizationSpeakers(self, value: int) -> None:
        normalized = max(0, int(value))
        if normalized != self._diarization_speakers:
            self._diarization_speakers = normalized
            self.formChanged.emit()

    @Property(str, notify=formChanged)
    def diarizationAlignment(self) -> str:
        return self._diarization_alignment

    @diarizationAlignment.setter
    def diarizationAlignment(self, value: str) -> None:
        normalized = value.strip().lower()
        if normalized not in {"basic", "precise"}:
            return
        if normalized != self._diarization_alignment:
            self._diarization_alignment = normalized
            self.formChanged.emit()

    @Property(str, notify=formChanged)
    def diarizationDevice(self) -> str:
        return self._diarization_device

    @diarizationDevice.setter
    def diarizationDevice(self, value: str) -> None:
        normalized = value.strip().lower()
        if normalized not in {"auto", "cuda", "cpu"}:
            return
        if normalized != self._diarization_device:
            self._diarization_device = normalized
            self.formChanged.emit()

    @Property(bool, notify=formChanged)
    def diarizationSrtLabels(self) -> bool:
        return self._diarization_srt_labels

    @diarizationSrtLabels.setter
    def diarizationSrtLabels(self, value: bool) -> None:
        normalized = bool(value)
        if normalized != self._diarization_srt_labels:
            self._diarization_srt_labels = normalized
            self.formChanged.emit()

    @Property(bool, notify=formChanged)
    def diarizationReady(self) -> bool:
        return self._diarization_ready

    @Property(bool, notify=formChanged)
    def diarizationBusy(self) -> bool:
        return self._diarization_busy

    @Property(str, notify=formChanged)
    def diarizationStatus(self) -> str:
        return self._t(self._raw_diarizationStatus())

    def _raw_diarizationStatus(self) -> str:
        return self._diarization_status

    @Property(bool, notify=formChanged)
    def canStart(self) -> bool:
        return (
            not self._busy
            and Path(self._video_path).is_file()
            and bool(self._code)
            and bool(self._output_dir)
            and bool(self._model)
            and (not self._diarization_enabled or self._diarization_ready)
        )

    @Property(str, notify=stateChanged)
    def taskTitle(self) -> str:
        return self._t(self._raw_taskTitle())

    def _raw_taskTitle(self) -> str:
        return self._task_title

    @Property(str, notify=stateChanged)
    def taskDetail(self) -> str:
        return self._t(self._raw_taskDetail())

    def _raw_taskDetail(self) -> str:
        return self._task_detail

    @Property(int, notify=stateChanged)
    def phaseIndex(self) -> int:
        return self._phase_index

    @Property(float, notify=stateChanged)
    def overallProgress(self) -> float:
        return self._overall_progress

    @Property(float, notify=stateChanged)
    def phaseProgress(self) -> float:
        return self._phase_progress

    @Property(str, notify=stateChanged)
    def elapsedText(self) -> str:
        return self._t(self._raw_elapsedText())

    def _raw_elapsedText(self) -> str:
        return self._elapsed_text

    @Property(str, notify=stateChanged)
    def etaText(self) -> str:
        return self._t(self._raw_etaText())

    def _raw_etaText(self) -> str:
        return self._eta_text

    @Property(str, notify=stateChanged)
    def lastSavedText(self) -> str:
        return self._t(self._raw_lastSavedText())

    def _raw_lastSavedText(self) -> str:
        return self._last_saved_text

    @Property(str, notify=stateChanged)
    def lastError(self) -> str:
        return self._t(self._raw_lastError())

    def _raw_lastError(self) -> str:
        return self._last_error

    @Property(str, notify=stateChanged)
    def systemSummary(self) -> str:
        return self._t(self._raw_systemSummary())

    def _raw_systemSummary(self) -> str:
        return self._system_summary

    @Property(str, notify=stateChanged)
    def contextLabel(self) -> str:
        return self._t(self._raw_contextLabel())

    def _raw_contextLabel(self) -> str:
        labels = {
            "home": "새 자막 작업",
            "running": f"{self._code or '작업'} · 실행 중",
            "jobs": "이전 작업",
            "diagnostic": "환경 진단",
            "settings": "고급 설정",
            "done": f"{self._code or '작업'} · 완료",
            "review": f"{self._code or '작업'} · 번역 검토",
        }
        return labels.get(self._screen, APP_NAME)

    @Property(list, notify=jobsChanged)
    def jobs(self) -> list[dict[str, Any]]:
        return self._t_list(self._raw_jobs())

    def _raw_jobs(self) -> list[dict[str, Any]]:
        return self._jobs

    @Property(bool, notify=jobsChanged)
    def resumeChoiceVisible(self) -> bool:
        return self._resume_choice_visible

    @Property(str, notify=jobsChanged)
    def resumeOriginalProfileText(self) -> str:
        return self._t(self._raw_resumeOriginalProfileText())

    def _raw_resumeOriginalProfileText(self) -> str:
        config = self._resume_original_config
        if config is None:
            return ""
        return (
            f"{config.provider} · {config.model} · "
            f"{config.reasoning_effort} · {config.speed_mode}"
        )

    @Property(str, notify=jobsChanged)
    def resumeChoiceText(self) -> str:
        return self._t(self._raw_resumeChoiceText())

    def _raw_resumeChoiceText(self) -> str:
        if self._resume_choice_blocked:
            return (
                "사용량 한도 또는 로그인 문제로 번역이 멈췄습니다. 완료된 배치는 유지하고 "
                "남은 배치만 다른 설정으로 이어갈 수 있습니다."
            )
        return (
            "중단된 작업을 이어갑니다. 완료된 전사와 번역 배치는 그대로 쓰고, 번역 설정을 바꾸면 "
            "남은 배치부터 새 설정을 적용합니다."
        )

    @Property(bool, notify=formChanged)
    def resumeSettingsMode(self) -> bool:
        return self._resume_settings_mode

    @Property(bool, notify=jobsChanged)
    def unverifiedBatchChoiceVisible(self) -> bool:
        return bool(self._unverified_batch_indexes and self._unverified_config)

    @Property(str, notify=jobsChanged)
    def unverifiedBatchText(self) -> str:
        return self._t(self._raw_unverifiedBatchText())

    def _raw_unverifiedBatchText(self) -> str:
        numbers = ", ".join(str(index + 1) for index in self._unverified_batch_indexes)
        return f"검증할 수 없는 기존 번역 배치: {numbers}" if numbers else ""

    @Property(list, notify=diagnosticsChanged)
    def diagnosticRows(self) -> list[dict[str, Any]]:
        return self._t_list(self._raw_diagnosticRows())

    def _raw_diagnosticRows(self) -> list[dict[str, Any]]:
        return self._diagnostic_rows

    @Property(str, notify=diagnosticsChanged)
    def diagnosticSummary(self) -> str:
        return self._t(self._raw_diagnosticSummary())

    def _raw_diagnosticSummary(self) -> str:
        return self._diagnostic_summary

    @Property(list, notify=logsChanged)
    def logs(self) -> list[dict[str, str]]:
        return self._t_list(self._raw_logs())

    def _raw_logs(self) -> list[dict[str, str]]:
        return self._logs

    @Property(list, notify=resultsChanged)
    def resultFiles(self) -> list[dict[str, Any]]:
        return self._result_files

    @Property(list, notify=resultsChanged)
    def resultMetrics(self) -> list[dict[str, str]]:
        return self._t_list(self._raw_resultMetrics())

    def _raw_resultMetrics(self) -> list[dict[str, str]]:
        return self._result_metrics

    @Slot(str)
    def showScreen(self, screen: str) -> None:
        if self._busy and screen not in {"running", "done"}:
            return
        if screen not in {
            "home",
            "running",
            "jobs",
            "diagnostic",
            "settings",
            "done",
            "review",
        }:
            return
        self._screen = screen
        if screen == "jobs":
            self.refreshJobs()
        self.stateChanged.emit()

    @Slot()
    def initialize(self) -> None:
        self.refreshJobs()
        self.refreshDiarizationStatus()
        if self._auto_diagnose:
            self.refreshAccounts()
        report = load_diagnostic_report()
        if report is not None:
            self._set_diagnostic_report(report)
        outdated = report is not None and report.hardware.app_version != APP_VERSION
        if (report is None or outdated) and self._auto_diagnose:
            self._screen = "diagnostic"
            QTimer.singleShot(0, self.runDiagnostic)
        self.stateChanged.emit()

    @Slot()
    def chooseVideo(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            None,
            self._t("입력 영상 선택"),
            str(Path(self._video_path).parent if self._video_path else Path.home()),
            self._t("영상 파일 (*.mp4 *.mkv *.mov *.avi *.wmv);;모든 파일 (*.*)"),
        )
        if selected:
            self._set_video_path(selected)

    @Slot(str)
    def acceptDroppedUrl(self, value: str) -> None:
        url = QUrl(value)
        path = url.toLocalFile() if url.isLocalFile() else value
        self._set_video_path(path)

    @Slot()
    def chooseOutput(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            None,
            self._t("출력 폴더 선택"),
            self._output_dir or str(Path.home()),
        )
        if selected:
            self.outputDir = selected

    def _set_video_path(self, value: str) -> None:
        normalized = value.strip()
        if normalized.startswith("file:"):
            normalized = QUrl(normalized).toLocalFile()
        path = Path(normalized).expanduser() if normalized else None
        self._video_path = str(path.resolve()) if path and path.exists() else normalized
        if path and path.is_file():
            self._code = infer_title(path)
            if not self._output_dir:
                self._output_dir_auto = True
            self._refresh_auto_output_dir()
            try:
                self._file_meta = _format_size(path.stat().st_size)
            except OSError:
                self._file_meta = "파일 크기를 확인할 수 없습니다."
        else:
            self._file_meta = "영상을 찾을 수 없습니다."
        self.formChanged.emit()

    def buildConfig(self) -> JobConfig:
        if not Path(self._video_path).is_file():
            raise ValueError("입력 영상을 선택하십시오.")
        if not self._output_dir:
            raise ValueError("출력 폴더를 선택하십시오.")
        if self._diarization_enabled and not self._diarization_ready:
            raise ValueError(
                "화자 분리 준비가 끝나지 않았습니다. 고급 설정에서 설치, 로그인과 "
                "모델 준비 상태를 확인하십시오."
            )
        compute_type = "int8" if self._accelerator == "cpu" else "auto"
        return JobConfig(
            video_path=Path(self._video_path),
            code=self._code,
            output_dir=Path(self._output_dir),
            provider=self._provider,
            model=self._model,
            reasoning_effort=self._reasoning,
            speed_mode=self._speed_mode,
            language_pair=self._language_pair,
            term_hints=self._term_hints.strip(),
            accelerator=self._accelerator,
            compute_type=compute_type,
            cpu_threads=self._cpu_threads or None,
            diarization_enabled=self._diarization_enabled,
            diarization_expected_speakers=self._diarization_speakers or None,
            diarization_alignment=self._diarization_alignment,
            diarization_device=self._diarization_device,
            diarization_srt_labels=self._diarization_srt_labels,
        )

    @Slot()
    def startJob(self) -> None:
        if self._busy:
            return
        try:
            config = self.buildConfig()
        except ValueError as exc:
            self._notify(str(exc))
            return
        self._start_job(config, resume=False)

    def _start_job(
        self,
        config: JobConfig,
        *,
        resume: bool,
        retry_held: bool = False,
        profile_switch_from: JobConfig | None = None,
    ) -> None:
        self._active_config = config
        self._cancel_event.clear()
        self._busy = True
        self._screen = "running"
        self._task_title = "환경을 확인하고 있습니다"
        self._task_detail = "영상, 전사 장치, 모델과 번역 로그인을 확인합니다."
        self._phase_index = 0
        self._overall_progress = 0.0
        self._phase_progress = 0.0
        self._last_error = ""
        self._session_started = time.monotonic()
        self._logs = []
        self.stateChanged.emit()
        self.logsChanged.emit()
        self._launch(
            "job",
            lambda: self._execute_job(
                config,
                resume=resume,
                retry_held=retry_held,
                profile_switch_from=profile_switch_from,
            ),
        )

    def _execute_job(
        self,
        config: JobConfig,
        *,
        resume: bool,
        retry_held: bool = False,
        profile_switch_from: JobConfig | None = None,
    ) -> tuple[JobConfig, ValidationResult]:
        del resume
        registry = self._registry
        profile_switch_authorized = profile_switch_from is None
        registry.upsert(config if profile_switch_authorized else profile_switch_from)
        try:
            if config.diarization_enabled:
                diarization_status = inspect_diarization_status()
                if not diarization_status.ready:
                    raise UserActionRequired(diarization_status.message)
            self._post_update(
                task_title="환경을 확인하고 있습니다",
                task_detail="필수 실행 환경과 선택한 번역 계정을 확인합니다.",
                phase_index=0,
                overall_progress=0.01,
                phase_progress=0.1,
            )
            report = run_full_diagnostic(
                video_path=config.video_path,
                selected_provider=config.provider,
                persist=True,
            )
            self._updateRequested.emit({"diagnostic_report": report})
            if not report.ready_to_start():
                blockers = [item.message for item in report.checks if not item.ok]
                raise UserActionRequired(blockers[0] if blockers else "환경 확인이 필요합니다.")
            if (
                report.recommendation.accelerator == "cpu"
                and config.accelerator == "auto"
            ):
                raise UserActionRequired(
                    "CUDA를 사용할 수 없습니다. 고급 설정에서 CPU 모드를 명시적으로 선택하십시오."
                )
            if profile_switch_from is not None:
                manifest = JobManifest.load_or_create(profile_switch_from)
                manifest.allow_translation_profile_switch(profile_switch_from, config)
                profile_switch_authorized = True
                registry.upsert(config)

            self._post_update(
                task_title=f"{config.direction.source_name} 음성을 전사하고 있습니다",
                task_detail="large-v3가 30초 단위로 체크포인트를 저장합니다.",
                phase_index=1,
                overall_progress=0.04,
                phase_progress=0.0,
            )
            transcribe(
                config,
                progress=self._logRequested.emit,
                progress_value=lambda fraction, message: self._stage_progress(
                    1, 0.04, 0.52, fraction, message
                ),
                cancel_event=self._cancel_event,
            )
            registry.upsert(config)

            self._post_update(
                task_title="직접 번역하고 있습니다",
                task_detail=f"{config.provider} · {config.model}의 응답을 기다립니다.",
                phase_index=2,
                overall_progress=0.56,
                phase_progress=0.0,
            )
            translate(
                config,
                retry_held=retry_held,
                codex_sessions=self._codex_sessions,
                parallel_batches=self._parallel_batches,
                progress_phase=self._phase_from_worker,
                progress=self._logRequested.emit,
                progress_value=lambda fraction, message: self._stage_progress(
                    2, 0.56, 0.39, fraction, message
                ),
                cancel_event=self._cancel_event,
            )
            registry.upsert(config)

            self._post_update(
                task_title="자막을 검증하고 있습니다",
                task_detail="타임코드, 원문 문자 잔존과 금지 표식을 확인합니다.",
                phase_index=3,
                overall_progress=0.96,
                phase_progress=0.2,
            )
            validation = build_and_validate(config)
            registry.upsert(config)
            self._post_update(overall_progress=1.0, phase_progress=1.0)
            return config, validation
        finally:
            registry.upsert(
                config
                if profile_switch_authorized or profile_switch_from is None
                else profile_switch_from
            )

    def _stage_progress(
        self,
        phase_index: int,
        base: float,
        weight: float,
        fraction: float | None,
        message: str,
    ) -> None:
        normalized = max(0.0, min(1.0, float(fraction or 0.0)))
        self._post_update(
            phase_index=phase_index,
            phase_progress=normalized,
            overall_progress=base + weight * normalized,
            task_detail=message,
            saved="저장" in message or "완료" in message,
        )

    @Slot()
    def stopJob(self) -> None:
        if not self._busy:
            return
        self._cancel_event.set()
        self._task_detail = "중지를 요청했습니다. 현재 안전 지점까지 저장합니다."
        self.stateChanged.emit()

    @Slot()
    def requestQuit(self) -> None:
        """Stop the running job and quit once it has reached a safe point."""

        if not (self._busy or self._review_busy):
            QCoreApplication.quit()
            return
        self._quit_requested = True
        self._cancel_event.set()
        self._task_detail = "작업을 안전하게 중지한 뒤 앱을 종료합니다."
        self.stateChanged.emit()
        QTimer.singleShot(int(SHUTDOWN_JOIN_SECONDS * 1000), self._force_quit)

    def _force_quit(self) -> None:
        if not self._quit_requested:
            return
        _logger.warning("작업이 제한 시간 안에 멈추지 않아 강제로 종료합니다.")
        terminate_active_processes()
        QCoreApplication.quit()

    @Slot()
    def shutdown(self) -> None:
        """Last-chance cleanup on exit: cancel, kill CLI children, join workers."""

        self._cancel_event.set()
        killed = terminate_active_processes()
        if killed:
            _logger.info("종료 중 번역 CLI 프로세스 %d개를 정리했습니다.", killed)
        # Keep the exit short: CLI children are already killed, transcription stops at
        # the next segment, and every checkpoint write is atomic.
        deadline = time.monotonic() + EXIT_JOIN_SECONDS
        for worker in list(self._workers):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
            if worker.is_alive():
                _logger.warning("작업 스레드 %s가 종료 시간 안에 끝나지 않았습니다.", worker.name)
        terminate_active_processes()

    @Slot()
    def loadReviewItems(self) -> None:
        config = self._review_config or self._active_config
        if config is None:
            self._review_items = []
        else:
            try:
                self._review_items = pending_review_items(config)
            except Exception as exc:
                _logger.exception("검토 항목을 읽지 못했습니다.")
                self._review_items = []
                self._notify(f"검토 항목을 읽을 수 없습니다: {exc}")
        self.reviewChanged.emit()

    @Slot(int)
    def retryReviewItem(self, raw_id: int) -> None:
        config = self._review_config
        if config is None or self._busy or self._review_busy:
            return
        self._review_busy = True
        self._cancel_event.clear()
        self.reviewChanged.emit()
        self._launch(
            "review_retry",
            lambda: retry_review_item(
                config,
                int(raw_id),
                cancel_event=self._cancel_event,
                progress=self._logRequested.emit,
            ),
        )

    @Slot(int, str, str, result=bool)
    def saveManualTranslation(self, raw_id: int, ja_fixed: str, ko: str) -> bool:
        config = self._review_config
        if config is None or self._busy or self._review_busy:
            return False
        try:
            save_manual_translation(config, int(raw_id), ja_fixed, ko)
        except (OSError, ValueError) as exc:
            self._notify(str(exc))
            return False
        self._notify(f"raw_id {raw_id}의 직접 입력을 저장했습니다.")
        self.loadReviewItems()
        return True

    def _review_action(self, action: Callable[[JobConfig], str | None]) -> bool:
        config = self._review_config
        if config is None or self._busy or self._review_busy:
            return False
        try:
            message = action(config)
        except (OSError, ValueError) as exc:
            self._notify(str(exc))
            return False
        if message:
            self._notify(message)
        self.loadReviewItems()
        return True

    @Slot(int, result=bool)
    def acceptReviewItem(self, raw_id: int) -> bool:
        def run(config: JobConfig) -> str:
            accepted, _ = accept_model_translations(config, [int(raw_id)])
            if not accepted:
                raise ValueError(
                    "이 줄은 채택할 모델 번역이 없거나 원문 문자가 남아 있습니다. "
                    "번역 자막 칸을 고쳐 저장하거나 삭제하십시오."
                )
            return f"raw_id {raw_id}: 모델 번역을 채택했습니다."

        return self._review_action(run)

    @Slot(int, result=bool)
    def omitReviewItem(self, raw_id: int) -> bool:
        return self._review_action(
            lambda config: (
                omit_review_items(config, [int(raw_id)])
                and f"raw_id {raw_id}: 자막에서 삭제했습니다."
            )
        )

    @Slot(result=bool)
    def acceptAllReviewItems(self) -> bool:
        def run(config: JobConfig) -> str:
            accepted, skipped = accept_model_translations(config)
            text = f"모델 번역 {accepted}개를 채택했습니다."
            if skipped:
                text += (
                    f" {len(skipped)}개는 채택할 번역이 없거나 원문 문자가 남아 그대로 두었습니다."
                )
            return text

        return self._review_action(run)

    @Slot(result=bool)
    def omitAllUnresolvedReviewItems(self) -> bool:
        ids = [int(item["raw_id"]) for item in self._review_items if not item.get("resolved")]
        return self._review_action(
            lambda config: f"남은 {omit_review_items(config, ids)}개를 자막에서 삭제했습니다."
        )

    @Property(int, notify=reviewChanged)
    def reviewAcceptableCount(self) -> int:
        return sum(
            1
            for item in self._review_items
            if not item.get("resolved") and item.get("kind") == "review" and item.get("ko")
        )

    @Property(str, notify=reviewChanged)
    def reviewJobKey(self) -> str:
        config = self._review_config
        return f"{config.output_dir}|{config.code}" if config else ""

    @Slot()
    def finishReview(self) -> None:
        """Re-run the job; cached batches are reused and resolutions are applied."""

        config = self._review_config
        if config is None or self._busy or self._review_busy:
            return
        if self.reviewUnresolvedCount:
            self._notify(
                "해결되지 않은 항목이 남아 있습니다. 다시 번역하거나 직접 입력하십시오."
            )
            return
        self._start_job(config, resume=True)

    @Property(bool, notify=reviewChanged)
    def reviewHasHeld(self) -> bool:
        return any(
            item.get("kind") == "held" and not item.get("resolved")
            for item in self._review_items
        )

    @Slot()
    def retryHeldBatches(self) -> None:
        """Explicit user action: send held batches again, each in a fresh session."""

        config = self._review_config
        if config is None or self._busy or self._review_busy:
            return
        self._start_job(config, resume=True, retry_held=True)

    def _open_review(self, config: JobConfig) -> None:
        self._review_config = config
        self._screen = "review"
        self.loadReviewItems()

    @Slot()
    def refreshJobs(self) -> None:
        try:
            records = self._registry.list_jobs()
            self._jobs = [self._job_view(item) for item in records]
        except Exception as exc:
            self._jobs = []
            self._notify(f"작업 목록을 읽을 수 없습니다: {exc}")
        self.jobsChanged.emit()

    def _job_view(self, record: JobRecord) -> dict[str, Any]:
        summary = record.to_summary()
        stage = record.last_completed_stage
        if record.completed_batches:
            progress = f"번역 {len(record.completed_batches)}개 배치 저장"
        elif record.completed_clips:
            progress = f"전사 {len(record.completed_clips)}개 클립 저장"
        else:
            progress = "작업 시작 전"
        return {
            **summary,
            "file_name": record.video_path.name,
            "stage_title": progress,
            "stage_detail": stage,
            "elapsed_text": _format_duration(record.active_elapsed_seconds),
            "updated_text": record.updated_at[:16].replace("T", " "),
        }

    @Slot(str)
    def resumeJob(self, job_id: str) -> None:
        if self._busy:
            return
        try:
            inspection = self._registry.inspect_resume(job_id, check_environment=False)
            if not inspection.can_resume:
                problems = [item.message for item in inspection.checks if not item.ok]
                self._notify(problems[0] if problems else "작업을 재개할 수 없습니다.")
                return
            config = inspection.job.job_config()
            block_kind = _translation_block_kind(config)
            if (
                inspection.job.status == "TRANSLATION_BLOCKED"
                and block_kind
                in {
                    ProviderFailureKind.USAGE_LIMIT.value,
                    ProviderFailureKind.AUTH_REQUIRED.value,
                }
            ):
                self._resume_original_config = config
                self._resume_choice_blocked = True
                self._resume_choice_visible = True
                self.jobsChanged.emit()
                return
            inspection = self._registry.inspect_resume(job_id)
            if not inspection.can_resume:
                problems = [item.message for item in inspection.checks if not item.ok]
                self._notify(problems[0] if problems else "작업을 재개할 수 없습니다.")
                return
        except Exception as exc:
            self._notify(f"작업을 재개할 수 없습니다: {exc}")
            return
        # Every resume offers keeping or changing the translation settings.
        self._resume_original_config = config
        self._resume_choice_blocked = False
        self._resume_choice_visible = True
        self.jobsChanged.emit()

    @Slot()
    def resumeWithSavedSettings(self) -> None:
        config = self._resume_original_config
        if config is None or self._busy:
            return
        self._resume_choice_visible = False
        self._resume_settings_mode = False
        self._load_config(config)
        self.jobsChanged.emit()
        self._start_job(config, resume=True)

    @Slot()
    def editResumeTranslationSettings(self) -> None:
        config = self._resume_original_config
        if config is None or self._busy:
            return
        self._resume_choice_visible = False
        self._resume_settings_mode = True
        self._load_config(config)
        self._screen = "home"
        self.jobsChanged.emit()
        self.stateChanged.emit()

    @Slot()
    def cancelResumeChoice(self) -> None:
        self._resume_choice_visible = False
        self._resume_original_config = None
        self.jobsChanged.emit()

    @Slot()
    def cancelResumeSettings(self) -> None:
        if self._busy:
            return
        self._resume_settings_mode = False
        self._resume_original_config = None
        self._screen = "jobs"
        self.refreshJobs()
        self.formChanged.emit()
        self.stateChanged.emit()

    @Slot()
    def startResumeWithChangedSettings(self) -> None:
        previous = self._resume_original_config
        if previous is None or not self._resume_settings_mode or self._busy:
            return
        try:
            config = self.buildConfig()
        except ValueError as exc:
            self._notify(str(exc))
            return
        old_profile = (
            previous.provider,
            previous.model,
            previous.reasoning_effort,
            previous.speed_mode,
        )
        new_profile = (
            config.provider,
            config.model,
            config.reasoning_effort,
            config.speed_mode,
        )
        if old_profile == new_profile:
            self._notify("번역 설정이 바뀌지 않아 기존 설정으로 이어서 진행합니다.")
            self.resumeWithSavedSettings()
            return

        self._busy = True
        self._task_title = "번역 계정을 확인하고 있습니다"
        self._task_detail = f"{config.provider} 로그인 상태를 확인합니다."
        self.stateChanged.emit()

        def check_profile() -> tuple[JobConfig, JobConfig]:
            status = get_provider(config.provider).status()
            if not status.ok:
                raise UserActionRequired(status.message)
            return previous, config

        self._launch("resume_profile_check", check_profile)

    @Slot()
    def confirmRetranslateUnverifiedBatches(self) -> None:
        config = self._unverified_config
        indexes = list(self._unverified_batch_indexes)
        if config is None or not indexes or self._busy:
            return
        manifest = JobManifest.load_or_create(config)
        manifest.allow_unverified_batch_retranslation(indexes)
        self._unverified_batch_indexes = []
        self._unverified_config = None
        self.jobsChanged.emit()
        self._start_job(config, resume=True)

    @Slot()
    def cancelUnverifiedBatchChoice(self) -> None:
        self._unverified_batch_indexes = []
        self._unverified_config = None
        self.jobsChanged.emit()

    @Slot(str)
    def removeJob(self, job_id: str) -> None:
        if self._busy:
            return
        try:
            self._registry.remove(job_id)
            self.refreshJobs()
        except Exception as exc:
            self._notify(f"목록에서 제거할 수 없습니다: {exc}")

    def _load_config(self, config: JobConfig) -> None:
        self._video_path = str(config.video_path)
        self._code = config.code
        self._output_dir = str(config.output_dir)
        self._output_dir_auto = False
        self._provider = config.provider
        self._model = config.model
        self._reasoning = config.reasoning_effort
        self._speed_mode = config.speed_mode
        self._language_pair = config.language_pair
        self._term_hints = config.term_hints
        self._accelerator = config.accelerator
        self._cpu_threads = config.cpu_threads or 0
        self._diarization_enabled = config.diarization_enabled
        self._diarization_speakers = config.diarization_expected_speakers or 0
        self._diarization_alignment = config.diarization_alignment
        self._diarization_device = config.diarization_device
        self._diarization_srt_labels = config.diarization_srt_labels
        self.formChanged.emit()

    @Slot()
    def refreshDiarizationStatus(self) -> None:
        status = inspect_diarization_status()
        self._diarization_available = not (
            is_frozen() and not status.runtime_ready
        )
        if not self._diarization_available:
            self._diarization_enabled = False
        self._diarization_ready = status.ready
        self._diarization_status = status.message
        self.formChanged.emit()

    @Slot()
    def installDiarizationDependencies(self) -> None:
        if self._busy or self._diarization_busy:
            return
        try:
            install_diarization_dependencies(Path(__file__).resolve().parents[2])
        except RuntimeError as exc:
            self._notify(str(exc))
            return
        self._notify(
            "화자 분리 구성 요소 설치 창을 열었습니다. 설치가 끝나면 상태를 다시 확인하십시오."
        )

    @Slot()
    def loginHuggingFace(self) -> None:
        if self._busy or self._diarization_busy:
            return
        if not self._diarization_available:
            self._notify(self._diarization_status)
            return
        login_huggingface()
        self._notify(
            "Hugging Face 로그인 창을 열었습니다. 토큰은 앱이 아닌 공식 CLI에 저장됩니다."
        )

    @Slot()
    def openDiarizationAgreement(self) -> None:
        if not self._diarization_available:
            self._notify(self._diarization_status)
            return
        open_official_page("diarization_agreement")

    @Slot()
    def downloadDiarizationModel(self) -> None:
        if self._busy or self._diarization_busy:
            return
        if not self._diarization_available:
            self._notify(self._diarization_status)
            return
        self._diarization_busy = True
        self._diarization_status = "community-1 모델을 준비하고 있습니다."
        self.formChanged.emit()
        self._launch("diarization_download", download_diarization_model)

    def _refresh_model_download_info(self) -> None:
        cache_dir = default_model_cache_dir()
        status = inspect_model_cache(cache_dir=cache_dir)
        size_gb = status.spec.estimated_download_bytes / 1_000_000_000
        self._model_download_size = f"약 {size_gb:.1f}GB"
        self._model_download_cache_path = str(status.cache_dir)

        usage_target = status.cache_dir
        while not usage_target.exists() and usage_target.parent != usage_target:
            usage_target = usage_target.parent
        try:
            free_bytes = shutil.disk_usage(usage_target).free
        except OSError:
            self._model_download_free_space = "확인할 수 없음"
        else:
            self._model_download_free_space = _format_size(free_bytes)

    @Slot()
    def confirmModelDownload(self) -> None:
        """Start the large-v3 download after the QML confirmation dialog."""

        if self._busy or self._diagnostic_busy:
            return
        self._refresh_model_download_info()
        cache_dir = Path(self._model_download_cache_path)
        self._diagnostic_busy = True
        self.diagnosticsChanged.emit()
        self._launch(
            "model_download",
            lambda: download_model(cache_dir=cache_dir, confirmed=True),
        )

    @Slot()
    def runDiagnostic(self) -> None:
        if self._busy or self._diagnostic_busy:
            return
        self._diagnostic_busy = True
        self._diagnostic_summary = "환경을 진단하고 있습니다."
        self.diagnosticsChanged.emit()
        video = Path(self._video_path) if Path(self._video_path).is_file() else None
        self._launch(
            "diagnostic",
            lambda: run_full_diagnostic(
                video_path=video,
                selected_provider=self._provider,
                persist=True,
            ),
        )

    @Slot(str)
    def performDiagnosticAction(self, action: str) -> None:
        try:
            if action == "download_model":
                self._notify(
                    "전사 모델의 용량과 저장 위치를 확인한 뒤 다운로드를 승인하십시오."
                )
                return
            if action == "install_ffmpeg":
                install_ffmpeg()
            elif action == "install_codex":
                install_codex()
            elif action == "install_claude":
                install_claude()
            elif action == "install_python":
                install_python_dependencies(Path(__file__).resolve().parents[2])
            elif action == "login_codex":
                get_provider("codex").launch_login()
            elif action == "login_claude":
                get_provider("claude").launch_login()
            elif action in {"open_cuda", "open_nvidia"}:
                open_official_page("cuda" if action == "open_cuda" else "nvidia")
            elif action == "use_cpu":
                self.accelerator = "cpu"
                self.showScreen("settings")
                return
            else:
                return
            self._notify("설치 또는 로그인 창을 열었습니다. 완료 후 다시 진단하십시오.")
        except Exception as exc:
            self._notify(str(exc))

    def _set_diagnostic_report(self, report: DiagnosticReport) -> None:
        self._refresh_model_download_info()
        rows: list[dict[str, Any]] = []
        hardware = report.hardware
        cpu_detail = f"{hardware.cpu_model} · {hardware.logical_cores}스레드"
        if hardware.avx2 is not None:
            cpu_detail += " · AVX2" if hardware.avx2 else " · AVX2 미확인"
        rows.append({"name": "프로세서", "detail": cpu_detail, "state": "확인됨", "action": ""})
        memory = (
            f"{hardware.total_memory_bytes / (1024**3):.1f} GB"
            if hardware.total_memory_bytes
            else "확인할 수 없음"
        )
        rows.append({"name": "메모리", "detail": memory, "state": "확인됨", "action": ""})
        gpu_names = [str(item.get("name", "GPU")) for item in hardware.gpu_devices]
        rows.append(
            {
                "name": "그래픽",
                "detail": ", ".join(gpu_names) if gpu_names else "전용 GPU를 찾지 못했습니다.",
                "state": "확인됨" if gpu_names else "CPU 사용 가능",
                "action": "",
            }
        )
        for check in report.checks:
            action = ""
            if not check.ok:
                if check.name == "model_large_v3":
                    action = "download_model"
                elif check.name == "ffprobe":
                    action = "install_ffmpeg"
                elif check.name == "python_dependencies":
                    action = "" if is_frozen() else "install_python"
                elif check.name == "cuda":
                    action = "open_cuda"
                elif check.name == "codex":
                    action = "install_codex" if check.status == "missing" else "login_codex"
                elif check.name == "claude":
                    action = "install_claude" if check.status == "missing" else "login_claude"
            labels = {
                "media": "입력 영상",
                "ffprobe": "FFmpeg",
                "python_dependencies": "전사 패키지",
                "storage": "저장 공간",
                "model_large_v3": "전사 모델",
                "cuda": "CUDA 실행",
                "cpu_runtime": "CPU 실행",
                "codex": "Codex 계정",
                "claude": "Claude 계정",
            }
            detail = check.message
            if check.name == "model_large_v3" and not check.ok:
                detail = (
                    f"{check.message} · {self._model_download_size} · "
                    f"저장 위치 {self._model_download_cache_path}"
                )
            rows.append(
                {
                    "name": labels.get(check.name, check.name),
                    "detail": detail,
                    "state": "확인됨" if check.ok else "조치 필요",
                    "problem": not check.ok,
                    "action": action,
                    "action_label": self._action_label(action),
                }
            )
        self._apply_account_checks(
            {check.name: check for check in report.checks if check.name in self._accounts}
        )
        self.accountsChanged.emit()
        rec = report.recommendation
        self._diagnostic_rows = rows
        self._diagnostic_summary = rec.reason
        if rec.accelerator == "cuda":
            self._system_summary = "CUDA 0 · large-v3 · float16"
        elif rec.accelerator == "cpu":
            self._system_summary = f"CPU · large-v3 · int8 · {rec.cpu_threads}스레드"
            rows.append(
                {
                    "name": "권장 대체",
                    "detail": "CUDA 대신 CPU int8을 명시적으로 선택할 수 있습니다.",
                    "state": "선택 필요",
                    "problem": True,
                    "action": "use_cpu",
                    "action_label": "CPU 사용",
                }
            )
        self.diagnosticsChanged.emit()
        self.stateChanged.emit()

    @staticmethod
    def _action_label(action: str) -> str:
        return {
            "download_model": "다운로드",
            "install_ffmpeg": "설치",
            "install_python": "설치",
            "install_codex": "설치",
            "install_claude": "설치",
            "login_codex": "로그인",
            "login_claude": "로그인",
            "open_cuda": "안내 열기",
            "use_cpu": "CPU 사용",
        }.get(action, "")

    @Slot()
    def saveSettings(self) -> None:
        self._settings.setValue("translation/provider", self._provider)
        self._settings.setValue("translation/model", self._model)
        self._settings.setValue("translation/reasoning", self._reasoning)
        self._settings.setValue("translation/speed", self._speed_mode)
        self._settings.setValue("transcription/accelerator", self._accelerator)
        self._settings.setValue("transcription/cpu_threads", self._cpu_threads)
        self._settings.setValue("diarization/enabled", self._diarization_enabled)
        self._settings.setValue(
            "diarization/expected_speakers", self._diarization_speakers
        )
        self._settings.setValue("diarization/alignment", self._diarization_alignment)
        self._settings.setValue("diarization/device", self._diarization_device)
        self._settings.setValue("diarization/srt_labels", self._diarization_srt_labels)
        self._settings.sync()
        self._notify("설정을 저장했습니다.")

    @Slot()
    def resetSettings(self) -> None:
        self._provider = "codex"
        self._model = "gpt-5.6-luna"
        self._reasoning = DEFAULT_REASONING["codex"]
        self._speed_mode = DEFAULT_SPEED["codex"]
        self._accelerator = "auto"
        self._cpu_threads = 0
        self._diarization_enabled = False
        self._diarization_speakers = 0
        self._diarization_alignment = "basic"
        self._diarization_device = "auto"
        self._diarization_srt_labels = False
        self.formChanged.emit()

    @Slot(str)
    def openPath(self, value: str) -> None:
        if value:
            QDesktopServices.openUrl(QUrl.fromLocalFile(value))

    @Slot()
    def openOutputFolder(self) -> None:
        if self._output_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._output_dir))

    @Slot()
    def newJob(self) -> None:
        if self._busy:
            return
        self._resume_choice_visible = False
        self._resume_settings_mode = False
        self._resume_original_config = None
        self._video_path = ""
        self._code = ""
        self._term_hints = ""
        self._output_dir = ""
        self._output_dir_auto = True
        self._file_meta = "영상을 선택하지 않았습니다."
        self._last_error = ""
        self._screen = "home"
        self.formChanged.emit()
        self.stateChanged.emit()

    def _launch(self, operation: str, target) -> None:
        def runner() -> None:
            try:
                result = target()
            except BaseException as exc:
                self._operationFinished.emit(operation, None, exc)
            else:
                self._operationFinished.emit(operation, result, None)

        worker = threading.Thread(target=runner, daemon=True, name=f"subtitle-{operation}")
        self._workers = [item for item in self._workers if item.is_alive()]
        self._workers.append(worker)
        self._worker = worker
        worker.start()

    def _post_update(self, **values: Any) -> None:
        self._updateRequested.emit(values)

    @Slot(dict)
    def _apply_update(self, values: dict[str, Any]) -> None:
        report = values.pop("diagnostic_report", None)
        if isinstance(report, DiagnosticReport):
            self._set_diagnostic_report(report)
        phase_event = values.pop("request_phase_event", None)
        if isinstance(phase_event, str):
            now = time.monotonic()
            if phase_event in {"번역 CLI 시작", "세션 연결"} and (
                self._request_started is None or self._first_event_seconds is not None
            ):
                self._request_started = now
                self._first_event_seconds = None
            elif self._request_started is not None and self._first_event_seconds is None:
                self._first_event_seconds = now - self._request_started
            self._request_phase = phase_event
        for name, value in values.items():
            if name == "saved":
                if value:
                    self._last_saved_text = "방금"
                continue
            attribute = f"_{name}"
            if hasattr(self, attribute):
                setattr(self, attribute, value)
        self.stateChanged.emit()

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self._logs.append({"time": datetime.now().strftime("%H:%M:%S"), "message": message})
        self._logs = self._logs[-80:]
        self.logsChanged.emit()

    @Slot(str, object, object)
    def _finish_operation(self, operation: str, result: object, error: object) -> None:
        expected_errors = (
            TranscriptionCancelled,
            TranslationCancelled,
            TranslationPartialError,
            TranslationReviewRequired,
            UnverifiableTranslationBatch,
            UserActionRequired,
        )
        if isinstance(error, BaseException) and not isinstance(error, expected_errors):
            _logger.error("작업 %s가 실패했습니다.", operation, exc_info=error)
        if operation in {"accounts", "account_logout"}:
            self._accounts_busy = False
            if isinstance(result, dict):
                self._apply_account_checks(result)
            if error is not None:
                self._notify(str(error))
            elif operation == "account_logout":
                self._notify("로그아웃했습니다.")
            self.accountsChanged.emit()
            return
        if operation == "resume_profile_check":
            self._busy = False
            if error is not None:
                self._notify(f"새 번역 설정을 사용할 수 없습니다: {error}")
                self.stateChanged.emit()
                return
            if not isinstance(result, tuple) or len(result) != 2:
                self._notify("번역 설정 확인 결과를 읽을 수 없습니다.")
                self.stateChanged.emit()
                return
            previous, config = result
            if not isinstance(previous, JobConfig) or not isinstance(config, JobConfig):
                self._notify("번역 설정 확인 결과가 올바르지 않습니다.")
                self.stateChanged.emit()
                return
            self._resume_settings_mode = False
            self._resume_original_config = None
            self.formChanged.emit()
            self._start_job(
                config,
                resume=True,
                profile_switch_from=previous,
            )
            return
        if operation == "review_retry":
            self._review_busy = False
            if error is not None:
                self._notify(f"다시 번역하지 못했습니다: {error}")
            elif isinstance(result, dict) and result.get("action") == "review":
                self._notify(
                    f"모델이 여전히 판단을 보류했습니다: {result.get('reason') or ''}"
                )
            else:
                self._notify("다시 번역한 결과를 저장했습니다.")
            self.loadReviewItems()
            if self._quit_requested:
                QCoreApplication.quit()
            return
        if operation == "diarization_download":
            self._diarization_busy = False
            self.refreshDiarizationStatus()
            if error is not None:
                message = str(error)
                self._diarization_status = message
                self._notify(message)
            else:
                self._notify("community-1 화자 분리 모델을 준비했습니다.")
            self.formChanged.emit()
            return
        if operation in {"diagnostic", "model_download"}:
            self._diagnostic_busy = False
            if error is not None:
                self._diagnostic_summary = str(error)
                self._notify(str(error))
            elif operation == "diagnostic" and isinstance(result, DiagnosticReport):
                self._set_diagnostic_report(result)
            else:
                status = inspect_model_cache()
                self._notify(
                    "전사 모델 다운로드를 완료했습니다."
                    if status.ready
                    else "전사 모델 상태를 다시 확인하십시오."
                )
                self.runDiagnostic()
            self.diagnosticsChanged.emit()
            return

        self._busy = False
        self._session_started = None
        self._request_started = None
        self._first_event_seconds = None
        self._request_phase = ""
        self._request_elapsed_text = ""
        if error is None and isinstance(result, tuple):
            config, validation = result
            if isinstance(config, JobConfig) and isinstance(validation, ValidationResult):
                self._load_config(config)
                self._load_results(config, validation)
                self._screen = "done"
                self._task_title = "작업이 끝났습니다"
                self._task_detail = "결과 파일을 저장하고 자동 검사를 마쳤습니다."
                self._overall_progress = 1.0
        else:
            message = _bounded_ui_message(error)
            self._last_error = message
            if isinstance(error, (TranscriptionCancelled, TranslationCancelled)):
                self._task_title = "작업을 중지했습니다"
                self._task_detail = "완료된 체크포인트를 보존했습니다."
                self._screen = "jobs"
            elif isinstance(error, (TranslationPartialError, TranslationReviewRequired)):
                self._task_title = "번역 검토가 필요합니다"
                self._task_detail = message
                if self._active_config is not None:
                    self._open_review(self._active_config)
                else:
                    self._screen = "jobs"
            elif isinstance(error, UnverifiableTranslationBatch):
                self._task_title = "기존 번역 배치를 확인할 수 없습니다"
                self._task_detail = message
                self._unverified_batch_indexes = list(error.batch_indexes)
                self._unverified_config = self._active_config
                self._screen = "jobs"
                self.jobsChanged.emit()
            elif isinstance(error, UserActionRequired):
                self._task_title = "환경 확인이 필요합니다"
                self._task_detail = message
                self._screen = "diagnostic"
            elif isinstance(error, ProviderError) and error.kind in {
                ProviderFailureKind.USAGE_LIMIT,
                ProviderFailureKind.AUTH_REQUIRED,
            }:
                self._task_title = (
                    "번역 사용량 한도에 도달했습니다"
                    if error.kind == ProviderFailureKind.USAGE_LIMIT
                    else "번역 계정 로그인이 필요합니다"
                )
                self._task_detail = message
                self._screen = "jobs"
            else:
                self._task_title = "작업을 완료하지 못했습니다"
                self._task_detail = message
                self._screen = "running"
            self._notify(message)
        self.refreshJobs()
        self.stateChanged.emit()
        if self._quit_requested:
            QCoreApplication.quit()

    def _load_results(self, config: JobConfig, validation: ValidationResult) -> None:
        paths = JobPaths(config)
        manifest = JobManifest.load_or_create(config)
        self._result_files = [
            {
                "name": path.name,
                "path": str(path),
                "size": _format_size(path.stat().st_size) if path.is_file() else "없음",
            }
            for path in paths.final_outputs
        ]
        values = manifest.stats
        self._result_metrics = [
            {"label": "원본 세그먼트", "value": str(values.get("raw_segments", 0))},
            {"label": "최종 자막 큐", "value": str(values.get("final_cues", 0))},
            {"label": "미매핑", "value": str(values.get("unmapped_segments", 0))},
            {"label": "제외", "value": str(values.get("omitted_segments", 0))},
            {"label": "타임코드 겹침", "value": str(validation.overlaps)},
            {"label": "‘잘 자’ 오탐", "value": str(validation.goodnight_false_positives)},
            {"label": "수동 검토 후보", "value": str(values.get("review_segments", 0))},
        ]
        if config.diarization_enabled and paths.raw.is_file():
            raw = load_json(paths.raw)
            speakers = raw.get("meta", {}).get("diarization", {}).get(
                "detected_speakers", []
            )
            self._result_metrics.append(
                {"label": "감지 화자", "value": str(len(speakers))}
            )
        self._elapsed_text = _format_duration(manifest.active_elapsed_seconds)
        self.resultsChanged.emit()

    @Slot()
    def _tick(self) -> None:
        if self._session_started is None:
            return
        if self._request_started is not None:
            self._request_elapsed_text = _format_duration(
                time.monotonic() - self._request_started
            )
        elapsed = time.monotonic() - self._session_started
        self._elapsed_text = _format_duration(elapsed)
        if self._overall_progress > 0.03:
            remaining = elapsed * (1.0 - self._overall_progress) / self._overall_progress
            self._eta_text = _format_duration(remaining)
        else:
            self._eta_text = "계산 중"
        self.stateChanged.emit()
