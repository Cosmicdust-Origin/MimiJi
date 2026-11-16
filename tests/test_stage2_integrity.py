"""Regression tests for pipeline integrity fixes (stage 2)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths, JobStatus
from local_subtitle_tool.diarization import DiarizationError, apply_diarization
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import ProviderError, ProviderFailureKind, ProviderResult
from local_subtitle_tool.subtitles import _build_cues, build_and_validate
from local_subtitle_tool.translator import (
    TranslationPartialError,
    TranslationReviewRequired,
    retry_review_item,
    save_manual_translation,
    translate,
)


def _item(raw_id: int, action: str = "translated", reason: str | None = None) -> dict:
    return {
        "raw_id": raw_id,
        "ja_fixed": f"日本語{raw_id}",
        "ko": f"한국어 {raw_id}" if action == "translated" else "",
        "action": action,
        "merge_with_previous": False,
        "reason": reason,
        "register_note": None,
    }


def _job(root: Path, decisions: list[dict] | None, *, code: str = "TEST-001") -> JobConfig:
    video = root / f"{code}.mp4"
    video.touch()
    config = JobConfig(video_path=video, code=code, output_dir=root / "out")
    paths = JobPaths(config)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.raw.write_text(
        json.dumps(
            {
                "meta": {"device": "cuda:0", "duration": 10.0, "vad_applied": True},
                "segments": [
                    {"id": i, "start": i * 2.0, "end": i * 2.0 + 1.0, "text": f"発話{i}"}
                    for i in range(3)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if decisions is not None:
        paths.decisions.write_text(
            json.dumps({"items": decisions}, ensure_ascii=False), encoding="utf-8"
        )
    return config


class ScriptedProvider:
    def __init__(self, script) -> None:
        self.script = script
        self.calls: list[list[int]] = []

    def status(self) -> CheckResult:
        return CheckResult("fake", True, "ready")

    def run(self, **kwargs) -> ProviderResult:
        payload = json.loads(str(kwargs["prompt"]).split("입력 데이터:\n", 1)[1])
        ids = list(payload["target_raw_ids"])
        self.calls.append(ids)
        result = self.script(ids, "previous_review" in payload)
        if isinstance(result, ProviderError):
            raise result
        return ProviderResult(payload={"items": result}, stdout="", stderr="", elapsed_seconds=0)


class PublishTests(unittest.TestCase):
    def test_outputs_are_published_only_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _job(root, [_item(i) for i in range(3)])
            paths = JobPaths(config)
            config.output_dir.mkdir(parents=True, exist_ok=True)
            paths.published_raw.write_text("legacy raw from another pipeline", encoding="utf-8")

            build_and_validate(config)

            for path in paths.final_outputs:
                self.assertTrue(path.is_file(), path)
            self.assertEqual(JobManifest.load_or_create(config).status, JobStatus.COMPLETED)
            published_raw = json.loads(paths.published_raw.read_text(encoding="utf-8"))
            self.assertEqual(len(published_raw["segments"]), 3)
            superseded = list(paths.superseded_dir.rglob(paths.raw_name))
            self.assertEqual(len(superseded), 1)
            self.assertIn("legacy raw", superseded[0].read_text(encoding="utf-8"))

    def test_review_state_retracts_previously_published_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _job(root, [_item(i) for i in range(3)])
            paths = JobPaths(config)
            build_and_validate(config)
            self.assertTrue(paths.ko_srt.is_file())

            paths.decisions.write_text(
                json.dumps(
                    {"items": [_item(0), _item(1, "review", "불명확"), _item(2)]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                build_and_validate(config)
            self.assertFalse(paths.ko_srt.exists())
            self.assertFalse(paths.ja_srt.exists())
            self.assertEqual(
                JobManifest.load_or_create(config).status, JobStatus.PARTIAL_REVIEW
            )
            self.assertTrue(list(paths.superseded_dir.rglob(paths.ko_srt.name)))

    def test_validation_failure_never_publishes_final_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bad = _item(1)
            bad["ko"] = "やめて"  # kana residue fails validation
            config = _job(root, [_item(0), bad, _item(2)])
            paths = JobPaths(config)
            with self.assertRaises(RuntimeError):
                build_and_validate(config)
            self.assertFalse(paths.ko_srt.exists())
            self.assertTrue(paths.staged(paths.ko_srt).exists())
            self.assertEqual(JobManifest.load_or_create(config).status, JobStatus.FAILED)

    def test_user_edited_published_file_is_kept_on_retract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _job(root, [_item(i) for i in range(3)])
            paths = JobPaths(config)
            build_and_validate(config)
            paths.ko_srt.write_text("user edited", encoding="utf-8")
            paths.decisions.write_text(
                json.dumps({"partial": True, "refused_raw_ids": [1], "items": []}),
                encoding="utf-8",
            )
            with self.assertRaises(RuntimeError):
                build_and_validate(config)
            self.assertEqual(paths.ko_srt.read_text(encoding="utf-8"), "user edited")
            self.assertFalse(paths.ja_srt.exists())


class CueMergeTests(unittest.TestCase):
    def test_merge_never_spans_an_omitted_utterance(self) -> None:
        segments = [
            {"id": 0, "start": 0.0, "end": 1.0, "text": "a"},
            {"id": 1, "start": 1.1, "end": 2.0, "text": "b"},
            {"id": 2, "start": 2.1, "end": 3.0, "text": "c"},
        ]
        decisions = [_item(0), _item(1, "omitted", "신음"), _item(2)]
        decisions[2]["merge_with_previous"] = True
        cues = _build_cues(segments, decisions)
        self.assertEqual([cue.raw_ids for cue in cues], [[0], [2]])


class HeldBatchPolicyTests(unittest.TestCase):
    def test_held_batch_is_not_resent_and_manual_input_is_kept(self) -> None:
        def script(ids, retry):
            if ids == [1]:
                return ProviderError("I cannot comply", kind=ProviderFailureKind.CONTENT_REFUSAL)
            return [_item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _job(Path(directory), None)
            config = JobConfig.from_dict({**config.to_dict(), "batch_size": 1})
            provider = ScriptedProvider(script)
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(TranslationPartialError),
            ):
                translate(config)
            save_manual_translation(config, 1, "発話1", "직접 입력 1")

            answering = ScriptedProvider(lambda ids, retry: [_item(i) for i in ids])
            with patch("local_subtitle_tool.translator.get_provider", return_value=answering):
                decisions = translate(config)
            self.assertEqual(answering.calls, [])
            self.assertEqual(decisions[1]["ko"], "직접 입력 1")

            # An explicit "retry held batches" re-sends it, but manual input still wins.
            with patch("local_subtitle_tool.translator.get_provider", return_value=answering):
                decisions = translate(config, retry_held=True)
            self.assertEqual(answering.calls, [[1]])
            self.assertEqual(decisions[1]["source"], "manual")

    def test_refused_review_group_is_not_resent_in_later_rounds_or_runs(self) -> None:
        def script(ids, retry):
            if retry:
                return ProviderError("I cannot comply", kind=ProviderFailureKind.CONTENT_REFUSAL)
            return [_item(i, "review", "불명확") if i in {0, 1} else _item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _job(Path(directory), None)
            provider = ScriptedProvider(script)
            for _ in range(2):
                with (
                    patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                    self.assertRaises(TranslationReviewRequired),
                ):
                    translate(config)
            self.assertEqual(provider.calls, [[0, 1, 2], [0, 1]])

    def test_user_retry_does_not_overwrite_manual_input(self) -> None:
        def script(ids, retry):
            return [_item(i, "review", "불명확") if i == 1 else _item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _job(Path(directory), None)
            with (
                patch(
                    "local_subtitle_tool.translator.get_provider",
                    return_value=ScriptedProvider(script),
                ),
                self.assertRaises(TranslationReviewRequired),
            ):
                translate(config)
            save_manual_translation(config, 1, "発話1", "직접 입력")
            with patch(
                "local_subtitle_tool.translator.get_provider",
                return_value=ScriptedProvider(lambda ids, retry: [_item(i) for i in ids]),
            ):
                retry_review_item(config, 1)
            resolutions = json.loads(JobPaths(config).resolutions.read_text(encoding="utf-8"))
            self.assertEqual(resolutions["items"]["1"]["source"], "manual")


class StageTwoReviewFixTests(unittest.TestCase):
    def test_user_retry_result_is_stored_as_resolution(self) -> None:
        def script(ids, retry):
            return [_item(i, "review", "불명확") if i == 1 else _item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _job(Path(directory), None)
            with (
                patch(
                    "local_subtitle_tool.translator.get_provider",
                    return_value=ScriptedProvider(script),
                ),
                self.assertRaises(TranslationReviewRequired),
            ):
                translate(config)
            with patch(
                "local_subtitle_tool.translator.get_provider",
                return_value=ScriptedProvider(lambda ids, retry: [_item(i) for i in ids]),
            ):
                retry_review_item(config, 1)
            resolutions = json.loads(JobPaths(config).resolutions.read_text(encoding="utf-8"))
            self.assertEqual(resolutions["items"]["1"]["source"], "review_retry_user")
            with patch(
                "local_subtitle_tool.translator.get_provider",
                return_value=ScriptedProvider(script),
            ):
                decisions = translate(config)
            self.assertEqual(decisions[1]["source"], "review_retry_user")

    def test_build_keeps_cue_count_and_validation_in_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _job(Path(directory), [_item(i) for i in range(3)])
            build_and_validate(config)
            stats = JobManifest.load_or_create(config).stats
            self.assertEqual(stats["final_cues"], 3)
            self.assertTrue(stats["validation"]["ok"])
            self.assertTrue(stats["published"]["files"])

    def test_translation_needing_review_retracts_earlier_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _job(Path(directory), [_item(i) for i in range(3)])
            paths = JobPaths(config)
            build_and_validate(config)
            self.assertTrue(paths.ko_srt.is_file())

            def script(ids, retry):
                return [_item(i, "review", "불명확") if i == 2 else _item(i) for i in ids]

            with (
                patch(
                    "local_subtitle_tool.translator.get_provider",
                    return_value=ScriptedProvider(script),
                ),
                self.assertRaises(TranslationReviewRequired),
            ):
                translate(config)
            self.assertFalse(paths.ko_srt.exists())
            self.assertFalse(paths.published_raw.exists())


class DiarizationErrorTests(unittest.TestCase):
    def test_unexpected_runner_error_becomes_diarization_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _job(root, None)
            config = JobConfig.from_dict({**config.to_dict(), "diarization_enabled": True})

            def broken_runner(*_args, **_kwargs):
                raise RuntimeError("torchcodec could not load FFmpeg")

            with self.assertRaises(DiarizationError) as raised:
                apply_diarization(config, runner=broken_runner, cancel_event=threading.Event())
            self.assertIn("화자 분리", str(raised.exception))
            self.assertNotIn("CUDA 전사", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
