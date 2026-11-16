from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths, JobStatus
from local_subtitle_tool.subtitles import (
    Cue,
    _render_srt,
    build_and_validate,
    export_outputs,
    validate_srt_pair,
)


class SubtitleTests(unittest.TestCase):
    def test_speaker_labels_are_hidden_by_default(self) -> None:
        cue = Cue(
            index=1,
            start=0.0,
            end=1.0,
            ja="はい。",
            ko="네.",
            raw_ids=[0],
            speaker="SPEAKER_00",
        )

        rendered = _render_srt([cue], "ko")

        self.assertNotIn("SPEAKER_00", rendered)

    def test_builds_matching_srt_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-001.mp4"
            video.touch()
            output = root / "output"
            config = JobConfig(video_path=video, code="TEST-001", output_dir=output)
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            raw = {
                "meta": {
                    "device": "cuda:0",
                    "model": "faster-whisper large-v3",
                    "compute_type": "float16",
                    "duration": 5.0,
                },
                "segments": [
                    {"id": 0, "start": 0.0, "end": 1.2, "text": "こんにちは"},
                    {"id": 1, "start": 1.1, "end": 2.0, "text": "大丈夫です"},
                ],
            }
            decisions = {
                "items": [
                    {
                        "raw_id": 0,
                        "ja_fixed": "こんにちは。",
                        "ko": "안녕하세요.",
                        "action": "translated",
                        "merge_with_previous": False,
                        "reason": None,
                        "register_note": "경어",
                    },
                    {
                        "raw_id": 1,
                        "ja_fixed": "大丈夫です。",
                        "ko": "괜찮습니다.",
                        "action": "translated",
                        "merge_with_previous": False,
                        "reason": None,
                        "register_note": "경어",
                    },
                ]
            }
            paths.raw.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            paths.decisions.write_text(json.dumps(decisions, ensure_ascii=False), encoding="utf-8")
            result = build_and_validate(config)
            self.assertTrue(result.ok)
            self.assertEqual(result.ko_cues, 2)
            self.assertTrue(paths.report.exists())
            self.assertEqual(validate_srt_pair(paths.ja_srt, paths.ko_srt).overlaps, 0)
            exported = export_outputs(config, root / "final")
            self.assertEqual(len(exported), 6)
            self.assertTrue(all(path.is_file() for path in exported))

    def test_rejects_japanese_residue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ja = root / "ja.srt"
            ko = root / "ko.srt"
            ja.write_text("1\n00:00:00,000 --> 00:00:01,000\nはい\n", encoding="utf-8")
            ko.write_text("1\n00:00:00,000 --> 00:00:01,000\nはい\n", encoding="utf-8")
            self.assertEqual(validate_srt_pair(ja, ko).japanese_residue, 1)

    def test_rejects_fragmentary_ellipsis_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ja = root / "ja.srt"
            ko = root / "ko.srt"
            ja.write_text("1\n00:00:00,000 --> 00:00:01,000\n違うと言っている\n", encoding="utf-8")
            ko.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n... 아니. ...라고... 거든요...\n",
                encoding="utf-8",
            )
            self.assertEqual(validate_srt_pair(ja, ko).junk_fragments, 1)

    def test_partial_translation_cannot_build_final_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-001.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="TEST-001", output_dir=root / "output")
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.raw.write_text(
                json.dumps(
                    {"segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "発話"}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            paths.decisions.write_text(
                json.dumps(
                    {"partial": True, "refused_raw_ids": [0], "items": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "거절된 번역 배치"):
                build_and_validate(config)
            manifest = JobManifest.load_or_create(config)
            self.assertEqual(manifest.status, JobStatus.TRANSLATION_PARTIAL)
            self.assertFalse(paths.ko_srt.exists())

    def test_speaker_change_blocks_merge_and_labels_are_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-003.mp4"
            video.touch()
            config = JobConfig(
                video_path=video,
                code="TEST-003",
                output_dir=root / "output",
                diarization_enabled=True,
                diarization_srt_labels=True,
            )
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.raw.write_text(
                json.dumps(
                    {
                        "meta": {
                            "duration": 3.0,
                            "diarization": {
                                "enabled": True,
                                "detected_speakers": ["SPEAKER_00", "SPEAKER_01"],
                            },
                        },
                        "segments": [
                            {
                                "id": 0,
                                "start": 0.0,
                                "end": 1.0,
                                "text": "はい",
                                "speaker": "SPEAKER_00",
                            },
                            {
                                "id": 1,
                                "start": 1.1,
                                "end": 2.0,
                                "text": "そうです",
                                "speaker": "SPEAKER_01",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            paths.decisions.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "raw_id": 0,
                                "ja_fixed": "はい。",
                                "ko": "네.",
                                "action": "translated",
                                "merge_with_previous": False,
                            },
                            {
                                "raw_id": 1,
                                "ja_fixed": "そうです。",
                                "ko": "그래요.",
                                "action": "translated",
                                "merge_with_previous": True,
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = build_and_validate(config)

            self.assertEqual(result.ko_cues, 2)
            ko_text = paths.ko_srt.read_text(encoding="utf-8-sig")
            self.assertIn("[SPEAKER_00] 네.", ko_text)
            self.assertIn("[SPEAKER_01] 그래요.", ko_text)


if __name__ == "__main__":
    unittest.main()
