from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from local_subtitle_tool.core import JobConfig, JobPaths
from local_subtitle_tool.diarization import (
    DiarizationTurn,
    apply_diarization,
    assign_speakers,
)


class SpeakerAssignmentTests(unittest.TestCase):
    def test_basic_alignment_uses_largest_overlap(self) -> None:
        turns = [
            DiarizationTurn(0.0, 2.0, "SPEAKER_00"),
            DiarizationTurn(2.0, 4.0, "SPEAKER_01"),
        ]
        segments = [
            {"id": 0, "start": 0.2, "end": 1.7, "text": "a", "words": []},
            {"id": 1, "start": 1.5, "end": 3.8, "text": "b", "words": []},
        ]

        assigned = assign_speakers(segments, turns, alignment="basic")

        self.assertEqual(assigned[0]["speaker"], "SPEAKER_00")
        self.assertEqual(assigned[1]["speaker"], "SPEAKER_01")

    def test_precise_alignment_assigns_words_and_segment_majority(self) -> None:
        turns = [
            DiarizationTurn(0.0, 1.8, "SPEAKER_00"),
            DiarizationTurn(1.8, 3.0, "SPEAKER_01"),
        ]
        segments = [
            {
                "id": 0,
                "start": 0.0,
                "end": 2.5,
                "text": "abc",
                "words": [
                    {"start": 0.1, "end": 0.8, "word": "a"},
                    {"start": 0.9, "end": 1.6, "word": "b"},
                    {"start": 2.0, "end": 2.4, "word": "c"},
                ],
            }
        ]

        assigned = assign_speakers(segments, turns, alignment="precise")

        self.assertEqual(assigned[0]["speaker"], "SPEAKER_00")
        self.assertEqual(
            [word["speaker"] for word in assigned[0]["words"]],
            ["SPEAKER_00", "SPEAKER_00", "SPEAKER_01"],
        )


class DiarizationCacheTests(unittest.TestCase):
    def test_cached_turns_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-001.mp4"
            video.touch()
            config = JobConfig(
                video_path=video,
                code="TEST-001",
                output_dir=root / "output",
                diarization_enabled=True,
                diarization_alignment="precise",
            )
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            paths.raw.write_text(
                json.dumps(
                    {
                        "meta": {"source_fingerprint": "source"},
                        "segments": [
                            {
                                "id": 0,
                                "start": 0.0,
                                "end": 1.0,
                                "text": "発話",
                                "words": [
                                    {"start": 0.0, "end": 1.0, "word": "発話"}
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            calls = 0

            def runner(_config, **_kwargs):
                nonlocal calls
                calls += 1
                return [DiarizationTurn(0.0, 1.0, "SPEAKER_00")], "cpu"

            apply_diarization(config, runner=runner)
            apply_diarization(config, runner=runner)

            self.assertEqual(calls, 1)
            payload = json.loads(paths.raw.read_text(encoding="utf-8"))
            self.assertEqual(payload["segments"][0]["speaker"], "SPEAKER_00")
            self.assertEqual(
                payload["meta"]["diarization"]["detected_speakers"],
                ["SPEAKER_00"],
            )

    def test_disabled_mode_removes_existing_speaker_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-002.mp4"
            video.touch()
            config = JobConfig(
                video_path=video,
                code="TEST-002",
                output_dir=root / "output",
            )
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            paths.raw.write_text(
                json.dumps(
                    {
                        "meta": {"diarization": {"enabled": True}},
                        "segments": [
                            {
                                "id": 0,
                                "start": 0.0,
                                "end": 1.0,
                                "text": "発話",
                                "speaker": "SPEAKER_00",
                                "words": [
                                    {
                                        "start": 0.0,
                                        "end": 1.0,
                                        "word": "発話",
                                        "speaker": "SPEAKER_00",
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            apply_diarization(config)

            payload = json.loads(paths.raw.read_text(encoding="utf-8"))
            self.assertNotIn("diarization", payload["meta"])
            self.assertNotIn("speaker", payload["segments"][0])
            self.assertNotIn("speaker", payload["segments"][0]["words"][0])


if __name__ == "__main__":
    unittest.main()
