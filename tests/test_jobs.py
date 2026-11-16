from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths, JobStatus
from local_subtitle_tool.jobs import JobRegistry


class JobRegistryTests(unittest.TestCase):
    def _config(self, root: Path, **changes: object) -> JobConfig:
        video = root / "TEST-001.mp4"
        video.touch(exist_ok=True)
        values: dict[str, object] = {
            "video_path": video,
            "code": "TEST-001",
            "output_dir": root / "output",
        }
        values.update(changes)
        return JobConfig(**values)  # type: ignore[arg-type]

    def test_registry_refreshes_progress_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            registry = JobRegistry(root / "app-data")
            record = registry.upsert(config)
            self.assertEqual(record.status, JobStatus.CREATED)

            manifest = JobManifest.load_or_create(config)
            manifest.completed_clips = [0, 1]
            manifest.completed_batches = [0]
            manifest.active_elapsed_seconds = 12.5
            manifest.set_status(JobStatus.TRANSLATING)

            refreshed = registry.list_jobs()[0]
            self.assertEqual(refreshed.completed_clips, (0, 1))
            self.assertEqual(refreshed.completed_batches, (0,))
            self.assertEqual(refreshed.active_elapsed_seconds, 12.5)
            self.assertEqual(refreshed.to_summary()["last_completed_clip"], 2)

    def test_valid_checkpoints_allow_resume_without_environment_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            paths = JobPaths(config)
            manifest = JobManifest.load_or_create(config)
            manifest.completed_clips = [0]
            manifest.completed_batches = [0]
            manifest.set_status(JobStatus.TRANSLATING)
            paths.clips_dir.mkdir(parents=True)
            paths.batches_dir.mkdir(parents=True)
            (paths.clips_dir / "000000.json").write_text(
                json.dumps(
                    {
                        "clip_index": 0,
                        "source_fingerprint": "source",
                        "settings_fingerprint": "settings",
                        "segments": [],
                    }
                ),
                encoding="utf-8",
            )
            (paths.batches_dir / "batch_0000.json").write_text(
                json.dumps(
                    {
                        "fingerprint": "translation",
                        "target_ids": [0],
                        "items": [{"raw_id": 0}],
                    }
                ),
                encoding="utf-8",
            )
            registry = JobRegistry(root / "app-data")
            record = registry.upsert(config)
            inspection = registry.inspect_resume(record.job_id, check_environment=False)
            self.assertTrue(inspection.can_resume)
            self.assertTrue(all(item.ok for item in inspection.checks))

    def test_corrupt_checkpoint_blocks_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            paths = JobPaths(config)
            manifest = JobManifest.load_or_create(config)
            manifest.completed_clips = [0]
            manifest.set_status(JobStatus.TRANSCRIBING)
            paths.clips_dir.mkdir(parents=True)
            (paths.clips_dir / "000000.json").write_text("{broken", encoding="utf-8")
            registry = JobRegistry(root / "app-data")
            record = registry.upsert(config)
            inspection = registry.inspect_resume(record.job_id, check_environment=False)
            self.assertFalse(inspection.can_resume)
            checkpoint = next(item for item in inspection.checks if item.name == "checkpoints")
            self.assertFalse(checkpoint.ok)

    def test_settings_mismatch_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = self._config(root)
            JobManifest.load_or_create(original)
            changed = self._config(root, reasoning_effort="high")
            registry = JobRegistry(root / "app-data")
            record = registry.upsert(changed)
            inspection = registry.inspect_resume(record.job_id, check_environment=False)
            settings = next(item for item in inspection.checks if item.name == "settings")
            self.assertFalse(settings.ok)

    def test_remove_only_removes_index_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            marker = config.output_dir / "keep.txt"
            marker.parent.mkdir(parents=True)
            marker.write_text("keep", encoding="utf-8")
            registry = JobRegistry(root / "app-data")
            record = registry.upsert(config)
            registry.remove(record.job_id)
            self.assertEqual(registry.list_jobs(include_completed=True), [])
            self.assertTrue(marker.is_file())

    def test_completed_jobs_are_hidden_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            manifest = JobManifest.load_or_create(config)
            manifest.set_status(JobStatus.COMPLETED)
            registry = JobRegistry(root / "app-data")
            registry.upsert(config)
            self.assertEqual(registry.list_jobs(), [])
            self.assertEqual(len(registry.list_jobs(include_completed=True)), 1)

    def test_one_corrupt_manifest_does_not_hide_other_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            healthy = self._config(root, output_dir=root / "healthy")
            broken = self._config(
                root,
                code="TEST-002",
                output_dir=root / "broken",
            )
            JobManifest.load_or_create(healthy)
            broken_paths = JobPaths(broken)
            broken_paths.state_dir.mkdir(parents=True)
            broken_paths.manifest.write_text("{broken", encoding="utf-8")
            registry = JobRegistry(root / "app-data")
            registry.upsert(healthy)
            broken_record = registry.upsert(broken)
            self.assertIsNotNone(broken_record.registry_error)
            jobs = registry.list_jobs(include_completed=True)
            self.assertEqual({item.code for item in jobs}, {"TEST-001", "TEST-002"})


if __name__ == "__main__":
    unittest.main()
