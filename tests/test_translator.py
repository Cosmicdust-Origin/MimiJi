from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths, JobStatus
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import ProviderError, ProviderFailureKind, ProviderResult
from local_subtitle_tool.translator import (
    TranslationCancelled,
    TranslationPartialError,
    _auto_omit,
    _batch_fingerprint,
    _make_prompt,
    _validate_items,
    translate,
)


def _translated_item(raw_id: int) -> dict[str, object]:
    return {
        "raw_id": raw_id,
        "ja_fixed": f"日本語{raw_id}",
        "ko": f"한국어 {raw_id}",
        "action": "translated",
        "merge_with_previous": False,
        "reason": None,
        "register_note": None,
    }


class RefusingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def status(self) -> CheckResult:
        return CheckResult("fake", True, "ready")

    def run(self, **kwargs: object) -> ProviderResult:
        del kwargs
        current = self.calls
        self.calls += 1
        if current == 1:
            raise ProviderError(
                "I cannot comply with this request",
                kind=ProviderFailureKind.CONTENT_REFUSAL,
                stdout="internal stdout",
                stderr="internal stderr",
                raw_response="I cannot comply with this request",
            )
        raw_id = 0 if current == 0 else 2
        return ProviderResult(
            payload={"items": [_translated_item(raw_id)]},
            stdout="",
            stderr="",
            elapsed_seconds=0.01,
        )


class TranslatorTests(unittest.TestCase):
    def test_narrow_goodnight_filter_requires_confidence(self) -> None:
        self.assertTrue(_auto_omit({"text": "おやすみ", "no_speech_prob": 0.8, "end": 30.0}))
        self.assertFalse(_auto_omit({"text": "おやすみ", "no_speech_prob": 0.05, "end": 12.0}))
        self.assertFalse(_auto_omit({"text": "やめて", "no_speech_prob": 0.9, "end": 30.0}))

    def test_mapping_must_be_exact_and_ordered(self) -> None:
        item = {
            "raw_id": 2,
            "ja_fixed": "やめて。",
            "ko": "그만해.",
            "action": "translated",
            "merge_with_previous": False,
            "reason": None,
            "register_note": None,
        }
        self.assertEqual(_validate_items({"items": [item]}, [2])[0]["ko"], "그만해.")
        with self.assertRaises(ValueError):
            _validate_items({"items": [item]}, [3])

    def test_batch_fingerprint_tracks_reasoning_and_speaker_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-001.mp4"
            video.touch()
            segments = [
                {
                    "id": 0,
                    "start": 0.0,
                    "end": 1.0,
                    "text": "こんにちは",
                    "speaker": "SPEAKER_00",
                }
            ]
            prompt = _make_prompt(
                code="TEST-001",
                all_segments=segments,
                target_ids=[0],
                context_size=0,
            )
            normal = JobConfig(video_path=video, code="TEST-001", output_dir=root / "out")
            high = JobConfig(
                video_path=video,
                code="TEST-001",
                output_dir=root / "out",
                reasoning_effort="high",
            )
            self.assertNotEqual(
                _batch_fingerprint(normal, prompt=prompt, target_ids=[0]),
                _batch_fingerprint(high, prompt=prompt, target_ids=[0]),
            )
            self.assertIn("SPEAKER_00", prompt)

    def test_refused_batch_is_isolated_while_later_batches_continue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-001.mp4"
            video.touch()
            config = JobConfig(
                video_path=video,
                code="TEST-001",
                output_dir=root / "out",
                batch_size=1,
            )
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.raw.write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "id": raw_id,
                                "start": float(raw_id),
                                "end": float(raw_id + 1),
                                "text": f"発話{raw_id}",
                                "no_speech_prob": 0.0,
                            }
                            for raw_id in range(3)
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            provider = RefusingProvider()
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(TranslationPartialError),
            ):
                translate(config)

            manifest = JobManifest.load_or_create(config)
            self.assertEqual(manifest.status, JobStatus.TRANSLATION_PARTIAL)
            self.assertEqual(manifest.completed_batches, [0, 2])
            refused = json.loads(paths.refused_batches.read_text(encoding="utf-8"))
            self.assertEqual(refused["batches"][0]["target_raw_ids"], [1])
            decisions = json.loads(paths.decisions.read_text(encoding="utf-8"))
            self.assertTrue(decisions["partial"])
            self.assertEqual([item["raw_id"] for item in decisions["items"]], [0, 2])
            events = paths.provider_events.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 3)
            failure = next(
                event
                for event in (json.loads(line) for line in events)
                if event["outcome"] == "failure"
            )
            self.assertEqual(failure["stdout"], "internal stdout")
            self.assertEqual(failure["stderr"], "internal stderr")

    def test_cancelled_translation_is_persisted_as_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-001.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="TEST-001", output_dir=root / "out")
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.raw.write_text(
                json.dumps(
                    {
                        "meta": {},
                        "segments": [
                            {
                                "id": 0,
                                "start": 0.0,
                                "end": 1.0,
                                "text": "こんにちは",
                                "no_speech_prob": 0.0,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cancel_event = threading.Event()
            cancel_event.set()
            with self.assertRaises(TranslationCancelled):
                translate(config, cancel_event=cancel_event)
            manifest = JobManifest.load_or_create(config)
            self.assertEqual(manifest.status, JobStatus.CANCELLED)
            self.assertEqual(manifest.stats.get("current_stage"), "translation")


if __name__ == "__main__":
    unittest.main()
