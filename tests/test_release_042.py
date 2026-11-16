from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from local_subtitle_tool.bridge import AppBridge
from local_subtitle_tool.diagnostics import (
    DIAGNOSTIC_SCHEMA_VERSION,
    DiagnosticReport,
    HardwareSnapshot,
    TranscriptionRecommendation,
)
from local_subtitle_tool.diarization import (
    DiarizationError,
    DiarizationStatus,
    _load_pipeline,
    inspect_diarization_status,
)
from local_subtitle_tool.jobs import JobRegistry
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import ProviderError, ProviderFailureKind
from local_subtitle_tool.qml_gui import create_engine

PACKAGED_DIARIZATION_MESSAGE = (
    "이 배포판에는 화자 분리가 포함되어 있지 않습니다. "
    "MimiJi-Diarization 배포판을 사용하십시오."
)


def _missing_packaged_diarization() -> DiarizationStatus:
    return DiarizationStatus(
        runtime_ready=False,
        authenticated=False,
        model_ready=False,
        ready=False,
        message=PACKAGED_DIARIZATION_MESSAGE,
    )


class Release042Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QQuickStyle.setStyle("Basic")
        cls.app = QApplication.instance() or QApplication([])

    def _bridge(self, root: Path, *, frozen: bool = False) -> AppBridge:
        settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
        if frozen:
            settings.setValue("diarization/enabled", True)
        with (
            patch("local_subtitle_tool.bridge.is_frozen", return_value=frozen),
            patch(
                "local_subtitle_tool.bridge.inspect_diarization_status",
                return_value=(
                    _missing_packaged_diarization()
                    if frozen
                    else DiarizationStatus(False, False, False, False, "설치 필요")
                ),
            ),
            patch.dict(os.environ, {"HF_HUB_CACHE": str(root / "hf-cache")}),
        ):
            return AppBridge(
                settings=settings,
                registry=JobRegistry(root / "registry"),
                auto_diagnose=False,
            )

    def test_frozen_base_build_reports_diarization_variant(self) -> None:
        with (
            patch("local_subtitle_tool.diarization._module_exists", return_value=False),
            patch("local_subtitle_tool.diarization.is_frozen", return_value=True),
        ):
            status = inspect_diarization_status()
            self.assertEqual(status.message, PACKAGED_DIARIZATION_MESSAGE)
            with self.assertRaisesRegex(
                DiarizationError, "MimiJi-Diarization 배포판"
            ):
                _load_pipeline()

    def test_frozen_base_build_disables_diarization_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bridge(Path(directory), frozen=True)
            self.assertFalse(bridge.diarizationAvailable)
            self.assertFalse(bridge.diarizationEnabled)
            bridge.diarizationEnabled = True
            self.assertFalse(bridge.diarizationEnabled)

            with (
                patch("local_subtitle_tool.bridge.is_frozen", return_value=True),
                patch(
                    "local_subtitle_tool.bridge.inspect_diarization_status",
                    return_value=_missing_packaged_diarization(),
                ),
            ):
                engine = create_engine(bridge)
            root = engine.rootObjects()[0]
            for object_name in (
                "diarizationEnabledChoice",
                "diarizationAgreementButton",
                "diarizationLoginButton",
                "diarizationDownloadButton",
            ):
                control = root.findChild(QObject, object_name)
                self.assertIsNotNone(control, object_name)
                self.assertFalse(control.property("enabled"), object_name)

    def test_model_download_requires_dialog_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root)
            cache_dir = root / "hf-cache"

            with (
                patch.dict(os.environ, {"HF_HUB_CACHE": str(cache_dir)}),
                patch.object(bridge, "_launch") as launch,
            ):
                bridge.performDiagnosticAction("download_model")
                launch.assert_not_called()

                bridge.confirmModelDownload()
                launch.assert_called_once()
                operation, target = launch.call_args.args
                self.assertEqual(operation, "model_download")

            with patch("local_subtitle_tool.bridge.download_model") as download:
                target()
            download.assert_called_once_with(cache_dir=cache_dir, confirmed=True)

    def test_model_diagnostic_shows_size_cache_and_free_space(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root)
            report = DiagnosticReport(
                schema_version=DIAGNOSTIC_SCHEMA_VERSION,
                created_at="2026-11-15T00:00:00",
                hardware=HardwareSnapshot(
                    cpu_model="Test CPU",
                    physical_cores=4,
                    logical_cores=8,
                    avx2=True,
                    total_memory_bytes=16 * 1024**3,
                    gpu_devices=(),
                    ctranslate2_version="4.8.2",
                    python_version="3.14",
                    app_version="0.4.2",
                ),
                hardware_fingerprint="test",
                checks=[
                    CheckResult(
                        "model_large_v3",
                        False,
                        "large-v3 모델을 받지 않았습니다.",
                    )
                ],
                recommendation=TranscriptionRecommendation(
                    available=True,
                    accelerator="cpu",
                    device_index=None,
                    compute_type="int8",
                    cpu_threads=4,
                    requires_confirmation=True,
                    reason="CPU 사용 여부를 선택하십시오.",
                ),
                selected_provider="codex",
            )

            with patch.dict(os.environ, {"HF_HUB_CACHE": str(root / "hf-cache")}):
                bridge._set_diagnostic_report(report)

            model_row = next(
                row for row in bridge.diagnosticRows if row["name"] == "전사 모델"
            )
            self.assertIn("약 3.1GB", model_row["detail"])
            self.assertIn(str(root / "hf-cache"), model_row["detail"])
            self.assertEqual(bridge.modelDownloadSize, "약 3.1GB")
            self.assertEqual(bridge.modelDownloadCachePath, str(root / "hf-cache"))
            self.assertTrue(bridge.modelDownloadFreeSpace)

            engine = create_engine(bridge)
            dialog = engine.rootObjects()[0].findChild(QObject, "modelDownloadDialog")
            self.assertIsNotNone(dialog)

    def test_usage_limit_error_returns_to_jobs_with_short_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bridge(Path(directory))
            bridge._busy = True
            bridge._screen = "running"
            error = ProviderError(
                "번역 모델 사용량 한도에 도달했습니다. 사용량이 회복된 뒤 이전 "
                "작업에서 이어서 진행하십시오.",
                kind=ProviderFailureKind.USAGE_LIMIT,
                stderr="internal raw stderr",
            )

            bridge._finish_operation("job", None, error)

            self.assertEqual(bridge.screen, "jobs")
            self.assertEqual(bridge.taskTitle, "번역 사용량 한도에 도달했습니다")
            self.assertLessEqual(len(bridge.taskDetail), 300)
            self.assertNotIn("internal raw stderr", bridge.lastError)


if __name__ == "__main__":
    unittest.main()
