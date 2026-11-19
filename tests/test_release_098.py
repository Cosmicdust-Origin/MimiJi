"""MimiJi 0.9.8: review idle acceptance, job sorting and completion sound."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from local_subtitle_tool.bridge import REVIEW_AUTO_ACCEPT_SECONDS, AppBridge
from local_subtitle_tool.core import JobConfig, JobManifest, JobStatus
from local_subtitle_tool.jobs import JobRegistry
from local_subtitle_tool.subtitles import ValidationResult


def _validation() -> ValidationResult:
    return ValidationResult(
        ok=True,
        ja_cues=1,
        ko_cues=1,
        timing_mismatches=0,
        overlaps=0,
        reversals=0,
        japanese_residue=0,
        forbidden_markers=0,
        junk_fragments=0,
        goodnight_false_positives=0,
        duration_overruns=0,
    )


class Release098Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QQuickStyle.setStyle("Basic")
        cls.app = QApplication.instance() or QApplication([])

    def _config(self, root: Path, code: str) -> JobConfig:
        video = root / f"{code}.mp4"
        video.touch()
        return JobConfig(video_path=video, code=code, output_dir=root / code)

    def _bridge(
        self, root: Path, *, registry: JobRegistry | None = None
    ) -> AppBridge:
        return AppBridge(
            settings=QSettings(
                str(root / "settings.ini"), QSettings.Format.IniFormat
            ),
            registry=registry or JobRegistry(root / "registry"),
            auto_diagnose=False,
        )

    def test_job_registry_supports_recent_completed_and_created_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = JobRegistry(root / "registry")
            cases = (
                (
                    "A",
                    JobStatus.TRANSLATING,
                    "2026-01-03T00:00:00+00:00",
                    "2026-01-05T00:00:00+00:00",
                ),
                (
                    "B",
                    JobStatus.COMPLETED,
                    "2026-01-02T00:00:00+00:00",
                    "2026-01-04T00:00:00+00:00",
                ),
                (
                    "C",
                    JobStatus.COMPLETED,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-06T00:00:00+00:00",
                ),
            )
            for code, status, created, updated in cases:
                config = self._config(root, code)
                manifest = JobManifest.load_or_create(config)
                manifest.status = status
                manifest.created_at = created
                manifest.updated_at = updated
                manifest.save()
                registry.upsert(config)

            def codes(sort_by: str) -> list[str]:
                return [
                    item.code
                    for item in registry.list_jobs(
                        include_completed=True, refresh=False, sort_by=sort_by
                    )
                ]

            self.assertEqual(codes("recent"), ["C", "A", "B"])
            self.assertEqual(codes("completed"), ["C", "B", "A"])
            self.assertEqual(codes("created"), ["A", "B", "C"])

    def test_job_filter_and_sort_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = JobRegistry(root / "registry")
            incomplete = self._config(root, "OPEN")
            completed = self._config(root, "DONE")
            JobManifest.load_or_create(incomplete)
            done_manifest = JobManifest.load_or_create(completed)
            done_manifest.status = JobStatus.COMPLETED
            done_manifest.save()
            registry.upsert(incomplete)
            registry.upsert(completed)

            bridge = self._bridge(root, registry=registry)
            bridge.refreshJobs()
            self.assertEqual(bridge.jobsFilter, "all")
            self.assertEqual({item["code"] for item in bridge.jobs}, {"OPEN", "DONE"})

            bridge.jobsFilter = "incomplete"
            bridge.jobsSort = "created"
            self.assertEqual([item["code"] for item in bridge.jobs], ["OPEN"])

            restored = self._bridge(root, registry=registry)
            self.assertEqual(restored.jobsFilter, "incomplete")
            self.assertEqual(restored.jobsSort, "created")

    def test_review_inactivity_accepts_model_text_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root)
            bridge._review_config = self._config(root, "REVIEW")
            bridge._screen = "review"
            bridge._review_items = [
                {"raw_id": 1, "kind": "review", "ko": "번역", "resolved": False}
            ]
            bridge._review_last_activity = time.monotonic() - REVIEW_AUTO_ACCEPT_SECONDS - 1
            bridge._review_idle_seconds = 1

            def mark_resolved() -> None:
                bridge._review_items = []

            with (
                patch(
                    "local_subtitle_tool.bridge.accept_model_translations",
                    return_value=(1, []),
                ) as accept,
                patch.object(bridge, "loadReviewItems", side_effect=mark_resolved),
                patch.object(bridge, "finishReview") as finish,
                patch(
                    "local_subtitle_tool.bridge.QTimer.singleShot",
                    side_effect=lambda _delay, callback: callback(),
                ),
            ):
                bridge._tick()

            accept.assert_called_once_with(bridge._review_config)
            finish.assert_called_once()

    def test_review_activity_resets_timer_but_held_only_review_does_not_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root)
            bridge._review_config = self._config(root, "REVIEW")
            bridge._screen = "review"
            bridge._review_items = [
                {"raw_id": 1, "kind": "review", "ko": "번역", "resolved": False}
            ]
            bridge.noteReviewActivity()
            self.assertTrue(bridge.reviewAutoAcceptActive)
            self.assertEqual(bridge.reviewAutoAcceptSeconds, REVIEW_AUTO_ACCEPT_SECONDS)

            bridge._review_items = [
                {"raw_id": 2, "kind": "held", "ko": "", "resolved": False}
            ]
            bridge.noteReviewActivity()
            self.assertFalse(bridge.reviewAutoAcceptActive)

    def test_successful_final_validation_plays_one_completion_sound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = self._bridge(root)
            config = self._config(root, "DONE")
            with (
                patch.object(bridge, "_load_results"),
                patch.object(bridge, "refreshJobs"),
                patch("local_subtitle_tool.bridge._play_completion_sound") as sound,
            ):
                bridge._finish_operation("job", (config, _validation()), None)

            sound.assert_called_once_with()
            self.assertEqual(bridge.screen, "done")


if __name__ == "__main__":
    unittest.main()
