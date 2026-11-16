"""MimiJi 0.8.8: VAD rescue pass, phrase-loop collapse, coverage report."""

from __future__ import annotations

import json
import unittest

from local_subtitle_tool.repetition import collapse_repeats
from local_subtitle_tool.subtitles import _coverage_text
from local_subtitle_tool.transcriber import (
    _needs_rescue,
    _rescue_reject_reason,
    _settings_fingerprint,
)


class CollapseTests(unittest.TestCase):
    def test_long_loops_keep_three(self) -> None:
        self.assertEqual(collapse_repeats("안 돼, " * 14), "안 돼, 안 돼, 안 돼, ")
        self.assertEqual(collapse_repeats("보세요, " * 5 + "보세요."), "보세요, 보세요, 보세요.")
        self.assertEqual(collapse_repeats("ダメ、" * 6 + "ダメ"), "ダメ、ダメ、ダメ")
        self.assertEqual(collapse_repeats("ダメ" * 5), "ダメダメダメ")
        self.assertEqual(
            collapse_repeats("그래, 그래, 그래, 그래, 그래, 자."), "그래, 그래, 그래, 자."
        )

    def test_short_or_natural_repetition_is_kept(self) -> None:
        samples = ("안 돼, 안 돼, 안 돼.", "진짜, 진짜.", "あああああああ", "하하하하하", "대단해.")
        for text in samples:
            self.assertEqual(collapse_repeats(text), text)


def _segment(text: str, start: float, end: float, **extra) -> dict:
    base = {
        "text": text,
        "start": start,
        "end": end,
        "no_speech_prob": 0.2,
        "avg_logprob": -0.4,
        "compression_ratio": 1.2,
    }
    base.update(extra)
    return base


class RescueTests(unittest.TestCase):
    def test_only_quiet_vad_and_loud_audio_windows_are_rescued(self) -> None:
        self.assertTrue(_needs_rescue(0.5, -35.0))
        self.assertFalse(_needs_rescue(10.0, -35.0))  # VAD already heard speech
        self.assertFalse(_needs_rescue(0.0, -60.0))  # genuinely quiet

    def test_rescue_filters(self) -> None:
        kept = [_segment("すごいね", 10.0, 11.0)]
        self.assertIsNone(_rescue_reject_reason(_segment("ずっといてください", 20.0, 22.0), kept))
        self.assertEqual(
            _rescue_reject_reason(_segment("あ" * 40, 30.0, 40.0, compression_ratio=8.0), kept),
            "repetition",
        )
        self.assertEqual(
            _rescue_reject_reason(_segment("嫌だ!", 1.0, 2.0, no_speech_prob=0.8), kept),
            "no_speech",
        )
        self.assertEqual(
            _rescue_reject_reason(_segment("何か", 1.0, 2.0, avg_logprob=-1.4), kept),
            "low_confidence",
        )
        self.assertEqual(
            _rescue_reject_reason(_segment("おやすみなさい", 50.0, 51.0), kept), "generic_phrase"
        )
        self.assertEqual(_rescue_reject_reason(_segment("別の言葉", 10.2, 10.9), kept), "overlap")
        self.assertEqual(_rescue_reject_reason(_segment("すごいね", 12.0, 13.0), kept), "duplicate")

    def test_new_policy_has_its_own_fingerprint(self) -> None:
        self.assertNotEqual(_settings_fingerprint(), _settings_fingerprint(legacy=True))


class CoverageTests(unittest.TestCase):
    def test_drop_is_flagged(self) -> None:
        segments = [{"id": i, "start": float(i * 10), "end": i * 10 + 1.0} for i in range(60)]
        segments.append({"id": 60, "start": 1500.0, "end": 1501.0, "rescue": True})
        raw = {
            "meta": {
                "duration": 2400.0,
                "rescue_pass": {
                    "windows": 3,
                    "kept_segments": 1,
                    "dropped_segments": {"no_speech": 2},
                },
            },
            "segments": segments,
        }
        decisions = [{"raw_id": i, "action": "translated"} for i in range(61)]
        text = _coverage_text(raw, decisions, [])
        self.assertIn("| 0~10분 | 60 | 0 | 60 | 0 |", text)
        self.assertIn("| 20~30분 | 1 | 1 | 1 | 0 |", text)
        self.assertIn("대사가 크게 줄어든 구간: 10~20분, 20~30분, 30~40분", text)
        self.assertIn("살린 줄 1개, 버린 줄 2개", text)


if __name__ == "__main__":
    unittest.main()


class ActiveTimeTests(unittest.TestCase):
    def test_concurrent_requests_are_not_added_up(self) -> None:
        import tempfile
        import threading
        import time
        from pathlib import Path
        from unittest.mock import patch

        from test_parallel_lanes import LaneProvider, compact

        from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths
        from local_subtitle_tool.translator import translate

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TIME-001.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="TIME-001", output_dir=root, batch_size=3)
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            segments = [
                {"id": i, "start": float(i), "end": i + 0.8, "text": f"発話{i}"}
                for i in range(9)
            ]
            paths.raw.write_text(json.dumps({"meta": {}, "segments": segments}), encoding="utf-8")
            barrier = threading.Barrier(3)

            def script(ids, n):
                barrier.wait(timeout=5)
                time.sleep(0.6)
                return compact(ids)

            with patch(
                "local_subtitle_tool.translator.get_provider", return_value=LaneProvider(script)
            ):
                translate(config, parallel_batches=3)
            elapsed = JobManifest.load_or_create(config).active_elapsed_seconds
            self.assertGreater(elapsed, 0.5)
            self.assertLess(elapsed, 1.2)  # three 0.6 s requests side by side, not 1.8 s


class WorkTitleTests(unittest.TestCase):
    def test_title_from_file_name(self) -> None:
        from pathlib import Path

        from local_subtitle_tool.core import infer_title, safe_file_stem

        self.assertEqual(infer_title(Path("ABC-043-【제목】①.mp4")), "ABC-043")
        self.assertEqual(
            infer_title(Path("My.Holiday.Trip.2025.1080p.WEB-DL.x264.mp4")), "My Holiday Trip 2025"
        )
        self.assertEqual(infer_title(Path("[Group] 우리 가족 여행 (2024).mkv")), "우리 가족 여행")
        self.assertEqual(safe_file_stem('a/b:c?*  title. '), "a_b_c__ title")
        self.assertEqual(safe_file_stem("XYZ-627"), "XYZ-627")

    def test_title_keeps_case_and_output_follows_title(self) -> None:
        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        from local_subtitle_tool.bridge import AppBridge
        from local_subtitle_tool.jobs import JobRegistry

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "family_trip.mp4"
            video.touch()
            bridge = AppBridge(
                settings=QSettings(str(root / "s.ini"), QSettings.Format.IniFormat),
                registry=JobRegistry(root / "reg"),
                auto_diagnose=False,
            )
            bridge.videoPath = str(video)
            self.assertEqual(bridge.code, "family trip")
            self.assertEqual(Path(bridge.outputDir), (root / "family trip").resolve())
            bridge.code = "Family Trip 2026"  # case kept, folder follows while automatic
            self.assertEqual(Path(bridge.outputDir), (root / "Family Trip 2026").resolve())
            bridge.outputDir = str(root / "custom")
            bridge.code = "Other"
            self.assertEqual(Path(bridge.outputDir), root / "custom")
            self.assertEqual(bridge.buildConfig().code, "Other")
