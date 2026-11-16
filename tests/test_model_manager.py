from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.model_manager import (
    MODEL_SPECS,
    download_model,
    inspect_model_cache,
)


class ModelManagerTests(unittest.TestCase):
    def _make_snapshot(self, root: Path, *, complete: bool) -> Path:
        spec = MODEL_SPECS["large-v3"]
        snapshot = (
            root
            / f"models--{spec.repo_id.replace('/', '--')}"
            / "snapshots"
            / "test-commit"
        )
        snapshot.mkdir(parents=True)
        filenames = spec.required_files if complete else spec.required_files[:-1]
        for filename in filenames:
            (snapshot / filename).write_bytes(b"model-data")
        return snapshot

    def test_complete_snapshot_is_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self._make_snapshot(root, complete=True)
            status = inspect_model_cache(cache_dir=root)
            self.assertTrue(status.ready)
            self.assertEqual(status.snapshot_dir, snapshot)
            self.assertEqual(status.missing_files, ())

    def test_incomplete_snapshot_reports_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._make_snapshot(root, complete=False)
            status = inspect_model_cache(cache_dir=root)
            self.assertFalse(status.ready)
            self.assertEqual(status.missing_files, ("vocabulary.json",))

    def test_download_requires_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PermissionError, "승인"):
                download_model(cache_dir=Path(directory))

    @patch("huggingface_hub.snapshot_download")
    def test_confirmed_download_is_verified(self, snapshot_download) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def create_snapshot(**_kwargs):
                return str(self._make_snapshot(root, complete=True))

            snapshot_download.side_effect = create_snapshot
            status = download_model(cache_dir=root, confirmed=True)
            self.assertTrue(status.ready)
            snapshot_download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
