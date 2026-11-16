from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_subtitle_tool.core import (
    MANIFEST_SCHEMA_VERSION,
    JobConfig,
    JobManifest,
    JobPaths,
    JobStatus,
    infer_code,
)


class CoreTests(unittest.TestCase):
    def test_infer_code(self) -> None:
        self.assertEqual(infer_code(Path("ABCD-904-title.mp4")), "ABCD-904")
        self.assertEqual(infer_code(Path("p-2997-title.mp4")), "P-2997")

    def test_manifest_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "CODE-001.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="code-001", output_dir=root / "out")
            manifest = JobManifest.load_or_create(config)
            manifest.stats["value"] = 7
            manifest.set_status(JobStatus.TRANSCRIBED)
            loaded = JobManifest.load_or_create(config)
            self.assertEqual(loaded.status, JobStatus.TRANSCRIBED)
            self.assertEqual(loaded.stats["value"], 7)
            self.assertEqual(loaded.schema_version, MANIFEST_SCHEMA_VERSION)

    def test_extended_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = JobConfig(
                video_path=root / "CODE-002.mp4",
                code="code-002",
                output_dir=root / "out",
                accelerator="cpu",
                device_index=1,
                compute_type="int8",
                cpu_threads=6,
                reasoning_effort="high",
                speed_mode="fast",
                diarization_enabled=True,
                diarization_expected_speakers=2,
                diarization_alignment="precise",
                diarization_device="cuda",
                diarization_srt_labels=True,
            )
            loaded = JobConfig.from_dict(config.to_dict())
            self.assertEqual(loaded, config)

    def test_v1_manifest_is_migrated_without_losing_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "CODE-003.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="CODE-003", output_dir=root / "out")
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.manifest.write_text(
                json.dumps(
                    {
                        "config": {
                            "video_path": str(video),
                            "code": "CODE-003",
                            "output_dir": str(config.output_dir),
                            "provider": "codex",
                            "model": "gpt-5.6-luna",
                            "batch_size": 180,
                            "context_size": 8,
                        },
                        "status": "TRANSLATING",
                        "completed_batches": [2, 0, 2, 1],
                        "created_at": "2026-11-13T12:00:00+00:00",
                        "updated_at": "2026-11-13T12:10:00+00:00",
                        "error": None,
                        "stats": {"raw_segments": 120},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            manifest = JobManifest.load_or_create(config)

            self.assertEqual(manifest.schema_version, MANIFEST_SCHEMA_VERSION)
            self.assertEqual(manifest.status, JobStatus.TRANSLATING)
            self.assertEqual(manifest.completed_batches, [0, 1, 2])
            self.assertEqual(manifest.completed_clips, [])
            self.assertEqual(manifest.active_elapsed_seconds, 0.0)
            self.assertEqual(manifest.stats["raw_segments"], 120)
            migrated_text = paths.manifest.read_text(encoding="utf-8")
            self.assertIn(f'"schema_version": {MANIFEST_SCHEMA_VERSION}', migrated_text)
            self.assertIn('"completed_clips": []', migrated_text)

    def test_future_manifest_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "CODE-004.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="CODE-004", output_dir=root / "out")
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.manifest.write_text(
                '{"schema_version": 999, "config": {}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "앱을 업데이트"):
                JobManifest.load_or_create(config)

    def test_invalid_runtime_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "CPU 스레드"):
                JobConfig(
                    video_path=root / "CODE-005.mp4",
                    code="CODE-005",
                    output_dir=root / "out",
                    cpu_threads=0,
                )
            with self.assertRaisesRegex(ValueError, "속도 모드"):
                JobConfig(
                    video_path=root / "CODE-005.mp4",
                    code="CODE-005",
                    output_dir=root / "out",
                    speed_mode="turbo",
                )

    def test_final_output_contract_has_six_stable_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = JobConfig(
                video_path=root / "CODE-006.mp4",
                code="CODE-006",
                output_dir=root / "out",
            )
            names = [path.name for path in JobPaths(config).final_outputs]
            self.assertEqual(
                names,
                [
                    "CODE-006_merged_raw_ja.json",
                    "CODE-006_resegmented_ja.srt",
                    "CODE-006_final_ko.srt",
                    "CODE-006_review.tsv",
                    "CODE-006_omitted_candidates.tsv",
                    "CODE-006_작업결과보고.md",
                ],
            )


if __name__ == "__main__":
    unittest.main()
