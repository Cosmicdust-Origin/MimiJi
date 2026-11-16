from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from local_subtitle_tool.bridge import AppBridge
from local_subtitle_tool.core import JobConfig
from local_subtitle_tool.jobs import JobRegistry
from local_subtitle_tool.qml_gui import create_engine


class AppBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QQuickStyle.setStyle("Basic")
        cls.app = QApplication.instance() or QApplication([])

    def _bridge(self, root: Path) -> AppBridge:
        settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
        bridge = AppBridge(
            settings=settings,
            registry=JobRegistry(root / "registry"),
            auto_diagnose=False,
        )
        return bridge

    def test_xhigh_is_default_and_marked_recommended_for_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bridge(Path(directory))
            self.assertEqual(bridge.reasoning, "xhigh")
            labels = bridge.reasoningLabels
            self.assertEqual(len(labels), len(bridge.reasoningOptions))
            self.assertEqual(labels[bridge.reasoningOptions.index("xhigh")], "xhigh (추천)")
            self.assertEqual(bridge.speedMode, "fast")
            self.assertIn("gpt-5.6-luna (추천)", bridge.modelLabels)
            self.assertEqual(bridge.speedLabels, ["standard", "fast (추천)"])
            bridge.provider = "claude"
            self.assertNotIn("(추천)", " ".join(bridge.reasoningLabels))
            self.assertEqual(bridge.speedMode, "standard")

    def test_build_config_uses_cpu_int8_and_selected_translation_options(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-001.mp4"
            video.touch()
            bridge = self._bridge(root)
            bridge.videoPath = str(video)
            bridge.outputDir = str(root / "output")
            bridge.provider = "claude"
            bridge.model = "opus"
            bridge.reasoning = "max"
            bridge.speedMode = "fast"
            bridge.accelerator = "cpu"
            bridge.cpuThreads = 7

            config = bridge.buildConfig()

            self.assertEqual(config.provider, "claude")
            self.assertEqual(config.model, "opus")
            self.assertEqual(config.reasoning_effort, "max")
            self.assertEqual(config.speed_mode, "standard")
            self.assertEqual(config.accelerator, "cpu")
            self.assertEqual(config.compute_type, "int8")
            self.assertEqual(config.cpu_threads, 7)

    def test_busy_job_blocks_navigation_away_from_running_screen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bridge(Path(directory))
            bridge._screen = "running"
            bridge._busy = True

            bridge.showScreen("settings")

            self.assertEqual(bridge.screen, "running")

    def test_build_config_includes_optional_diarization_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-004.mp4"
            video.touch()
            bridge = self._bridge(root)
            bridge.videoPath = str(video)
            bridge.outputDir = str(root / "output")
            bridge._diarization_ready = True
            bridge.diarizationEnabled = True
            bridge.diarizationSpeakers = 2
            bridge.diarizationAlignment = "precise"
            bridge.diarizationDevice = "cuda"
            bridge.diarizationSrtLabels = True

            config = bridge.buildConfig()

            self.assertTrue(config.diarization_enabled)
            self.assertEqual(config.diarization_expected_speakers, 2)
            self.assertEqual(config.diarization_alignment, "precise")
            self.assertEqual(config.diarization_device, "cuda")
            self.assertTrue(config.diarization_srt_labels)

    def test_refresh_jobs_exposes_saved_incomplete_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-002.mp4"
            video.touch()
            registry = JobRegistry(root / "registry")
            registry.upsert(
                JobConfig(
                    video_path=video,
                    code="TEST-002",
                    output_dir=root / "output",
                )
            )
            settings = QSettings(
                str(root / "settings.ini"),
                QSettings.Format.IniFormat,
            )
            bridge = AppBridge(
                settings=settings,
                registry=registry,
                auto_diagnose=False,
            )

            bridge.refreshJobs()

            self.assertEqual(len(bridge.jobs), 1)
            self.assertEqual(bridge.jobs[0]["code"], "TEST-002")
            self.assertEqual(bridge.jobs[0]["stage_title"], "작업 시작 전")

    def test_qml_application_loads_with_bridge_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self._bridge(Path(directory))
            engine = create_engine(bridge)
            self.addCleanup(engine.deleteLater)

            self.assertEqual(len(engine.rootObjects()), 1)
            self.assertEqual(engine.rootObjects()[0].title(), "MimiJi")


if __name__ == "__main__":
    unittest.main()
