from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QSettings

from local_subtitle_tool.bridge import AppBridge
from local_subtitle_tool.core import (
    MANIFEST_SCHEMA_VERSION,
    JobConfig,
    JobManifest,
    JobPaths,
    JobStatus,
)
from local_subtitle_tool.jobs import JobRegistry
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import (
    ProviderError,
    ProviderFailureKind,
    ProviderResult,
)
from local_subtitle_tool.subtitles import _translation_history
from local_subtitle_tool.translator import (
    TranslationPartialError,
    UnverifiableTranslationBatch,
    _batch_fingerprint,
    _make_prompt,
    load_resolutions,
    retry_review_item,
    save_manual_translation,
    translate,
)


def _item(raw_id: int) -> dict:
    return {
        "raw_id": raw_id,
        "ja_fixed": f"発話{raw_id}",
        "ko": f"발화 {raw_id}",
        "action": "translated",
        "merge_with_previous": False,
        "reason": None,
        "register_note": None,
    }


class FakeProvider:
    def __init__(self, script=None) -> None:
        self.calls: list[list[int]] = []
        self.script = script

    def status(self) -> CheckResult:
        return CheckResult("fake", True, "로그인됨")

    def run(self, **kwargs) -> ProviderResult:
        prompt = str(kwargs["prompt"])
        payload = json.loads(prompt.split("입력 데이터:\n", 1)[1])
        target_ids = [int(item) for item in payload["target_raw_ids"]]
        self.calls.append(target_ids)
        result = self.script(target_ids) if self.script else [_item(i) for i in target_ids]
        if isinstance(result, ProviderError):
            raise result
        return ProviderResult(
            payload={"items": result},
            stdout="",
            stderr="",
            elapsed_seconds=0.0,
        )


def _prepare(root: Path, *, count: int, model: str = "old-model") -> JobConfig:
    video = root / "TEST-050.mp4"
    video.touch()
    config = JobConfig(
        video_path=video,
        code="TEST-050",
        output_dir=root / "out",
        model=model,
        reasoning_effort="high",
        speed_mode="fast",
        batch_size=1,
    )
    paths = JobPaths(config)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.raw.write_text(
        json.dumps(
            {
                "meta": {},
                "segments": [
                    {
                        "id": raw_id,
                        "start": float(raw_id),
                        "end": float(raw_id) + 0.8,
                        "text": f"発話{raw_id}",
                        "no_speech_prob": 0.0,
                    }
                    for raw_id in range(count)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config


def _changed(config: JobConfig, model: str = "new-model") -> JobConfig:
    data = config.to_dict()
    data["model"] = model
    return JobConfig.from_dict(data)


class TranslationProfileResumeTests(unittest.TestCase):
    def test_model_change_without_opt_in_does_not_reuse_old_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = _prepare(Path(directory), count=2)
            first = FakeProvider()
            with patch("local_subtitle_tool.translator.get_provider", return_value=first):
                translate(old)

            new = _changed(old)
            second = FakeProvider()
            with patch("local_subtitle_tool.translator.get_provider", return_value=second):
                translate(new)

            self.assertEqual(second.calls, [[0], [1]])

    def test_profile_switch_reuses_completed_and_requests_only_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = _prepare(Path(directory), count=3)

            def stop_after_first(target_ids: list[int]):
                if target_ids == [1]:
                    return ProviderError(
                        "사용량 한도",
                        kind=ProviderFailureKind.USAGE_LIMIT,
                    )
                return [_item(i) for i in target_ids]

            with (
                patch(
                    "local_subtitle_tool.translator.get_provider",
                    return_value=FakeProvider(stop_after_first),
                ),
                self.assertRaises(ProviderError),
            ):
                translate(old)

            new = _changed(old)
            manifest = JobManifest.load_or_create(old)
            manifest.allow_translation_profile_switch(old, new)
            resumed = FakeProvider()
            with patch("local_subtitle_tool.translator.get_provider", return_value=resumed):
                decisions = translate(new)

            self.assertEqual(resumed.calls, [[1], [2]])
            self.assertEqual(decisions[0]["execution_profile"]["model"], "old-model")
            self.assertEqual(decisions[1]["execution_profile"]["model"], "new-model")

    def test_held_batch_is_not_resent_after_profile_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = _prepare(Path(directory), count=2)

            def refuse_first(target_ids: list[int]):
                if target_ids == [0]:
                    return ProviderError(
                        "거절",
                        kind=ProviderFailureKind.CONTENT_REFUSAL,
                    )
                return [_item(i) for i in target_ids]

            with (
                patch(
                    "local_subtitle_tool.translator.get_provider",
                    return_value=FakeProvider(refuse_first),
                ),
                self.assertRaises(TranslationPartialError),
            ):
                translate(old)

            new = _changed(old)
            JobManifest.load_or_create(old).allow_translation_profile_switch(old, new)
            resumed = FakeProvider()
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=resumed),
                self.assertRaises(TranslationPartialError),
            ):
                translate(new)

            self.assertEqual(resumed.calls, [])

    def test_schema_three_migrates_and_legacy_profile_validates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = _prepare(Path(directory), count=1)
            paths = JobPaths(old)
            JobManifest.load_or_create(old)
            payload = json.loads(paths.manifest.read_text(encoding="utf-8"))
            payload["schema_version"] = 3
            payload.pop("translation_resume", None)
            paths.manifest.write_text(json.dumps(payload), encoding="utf-8")

            migrated = JobManifest.load_or_create(old)
            self.assertEqual(migrated.schema_version, MANIFEST_SCHEMA_VERSION)
            self.assertFalse(
                migrated.translation_resume["reuse_completed_batches_across_profiles"]
            )

            segments = json.loads(paths.raw.read_text(encoding="utf-8"))["segments"]
            prompt = _make_prompt(
                code=old.code,
                all_segments=segments,
                target_ids=[0],
                context_size=old.context_size,
            )
            fingerprint = _batch_fingerprint(old, prompt=prompt, target_ids=[0])
            paths.batches_dir.mkdir(parents=True, exist_ok=True)
            paths.batches_dir.joinpath("batch_0000.json").write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "provider": old.provider,
                        "model": old.model,
                        "reasoning_effort": old.reasoning_effort,
                        "speed_mode": old.speed_mode,
                        "target_ids": [0],
                        "items": [_item(0)],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            new = _changed(old)
            migrated.allow_translation_profile_switch(old, new)
            provider = FakeProvider()
            with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
                decisions = translate(new)

            self.assertEqual(provider.calls, [])
            self.assertEqual(decisions[0]["execution_profile"]["model"], "old-model")

    def test_unverifiable_legacy_batch_requires_explicit_retranslation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = _prepare(Path(directory), count=1)
            paths = JobPaths(old)
            paths.batches_dir.mkdir(parents=True, exist_ok=True)
            paths.batches_dir.joinpath("batch_0000.json").write_text(
                json.dumps(
                    {
                        "fingerprint": "unknown",
                        "target_ids": [0],
                        "items": [_item(0)],
                    }
                ),
                encoding="utf-8",
            )
            new = _changed(old)
            manifest = JobManifest.load_or_create(old)
            manifest.allow_translation_profile_switch(old, new)
            with (
                patch(
                    "local_subtitle_tool.translator.get_provider",
                    return_value=FakeProvider(),
                ),
                self.assertRaises(UnverifiableTranslationBatch),
            ):
                translate(new)

            manifest = JobManifest.load_or_create(new)
            self.assertEqual(manifest.status, JobStatus.TRANSLATION_BLOCKED)
            manifest.allow_unverified_batch_retranslation([0])
            provider = FakeProvider()
            with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
                translate(new)
            self.assertEqual(provider.calls, [[0]])

    def test_report_groups_ranges_by_actual_execution_profile(self) -> None:
        decisions = []
        for raw_id, model, kind, stamp in (
            (0, "old-model", "batch", "2026-11-15T01:00:00+00:00"),
            (1, "old-model", "batch", "2026-11-15T01:00:00+00:00"),
            (2, "new-model", "review_single", "2026-11-15T02:00:00+00:00"),
        ):
            item = _item(raw_id)
            item.update(
                {
                    "execution_profile": {
                        "provider": "codex",
                        "model": model,
                        "reasoning_effort": "high",
                        "speed_mode": "fast",
                    },
                    "processing_kind": kind,
                    "processed_at": stamp,
                }
            )
            decisions.append(item)

        report = _translation_history(decisions)
        self.assertIn("raw_id 0–1: 일반 배치 · codex / old-model", report)
        self.assertIn("raw_id 2: 검토 보강 단건 · codex / new-model", report)

    def test_review_retry_and_manual_input_record_actual_profile_and_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _prepare(Path(directory), count=2)
            request_count = 0

            def review_then_resolve(target_ids: list[int]):
                nonlocal request_count
                request_count += 1
                if request_count == 1:
                    item = _item(0)
                    item.update({"action": "review", "ko": "", "reason": "불명확"})
                    return [item]
                return [_item(i) for i in target_ids]

            provider = FakeProvider(review_then_resolve)
            with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
                decisions = translate(config)
                retry_review_item(config, 0)
            save_manual_translation(config, 1, "発話1", "직접 입력 1")

            self.assertEqual(decisions[0]["processing_kind"], "review_small")
            self.assertEqual(decisions[0]["execution_profile"]["model"], "old-model")
            self.assertTrue(decisions[0]["processed_at"])
            resolutions = load_resolutions(JobPaths(config))
            self.assertEqual(resolutions[0]["processing_kind"], "review_retry_user")
            self.assertEqual(resolutions[0]["execution_profile"]["model"], "old-model")
            self.assertTrue(resolutions[0]["processed_at"])
            self.assertEqual(resolutions[1]["processing_kind"], "manual")
            self.assertEqual(resolutions[1]["execution_profile"]["provider"], "manual")
            self.assertTrue(resolutions[1]["processed_at"])


class ResumeChoiceBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_choice_is_shown_for_every_resume_with_reason_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _prepare(root, count=1)
            manifest = JobManifest.load_or_create(config)
            manifest.stats["translation_blocked_kind"] = "USAGE_LIMIT"
            manifest.set_status(JobStatus.TRANSLATION_BLOCKED, error="사용량 한도")
            registry = JobRegistry(root / "registry")
            record = registry.upsert(config)
            bridge = AppBridge(
                settings=QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat),
                registry=registry,
                auto_diagnose=False,
            )
            inspection = registry.inspect_resume(record.job_id, check_environment=False)
            with patch.object(registry, "inspect_resume", return_value=inspection):
                bridge.resumeJob(record.job_id)
            self.assertTrue(bridge.resumeChoiceVisible)
            self.assertIn("사용량 한도", bridge.resumeChoiceText)

            bridge.cancelResumeChoice()
            manifest = JobManifest.load_or_create(config)
            manifest.stats["translation_blocked_kind"] = "TRANSIENT_ERROR"
            manifest.set_status(JobStatus.TRANSLATION_BLOCKED, error="일시 오류")
            record = registry.upsert(config)
            inspection = registry.inspect_resume(record.job_id, check_environment=False)
            with (
                patch.object(registry, "inspect_resume", return_value=inspection),
                patch.object(bridge, "_start_job") as start_job,
            ):
                bridge.resumeJob(record.job_id)
            # 0.8.1: any resume lets the user keep or change the translation settings.
            self.assertTrue(bridge.resumeChoiceVisible)
            self.assertIn("중단된 작업", bridge.resumeChoiceText)
            start_job.assert_not_called()
            with patch.object(bridge, "_start_job") as start_job:
                bridge.resumeWithSavedSettings()
            start_job.assert_called_once()


if __name__ == "__main__":
    unittest.main()
