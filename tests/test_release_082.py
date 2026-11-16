"""MimiJi 0.8.2: stalled Codex requests and review reuse across speed modes."""

from __future__ import annotations

import json
import os
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_parallel_lanes import LaneProvider, ParallelLaneTests, compact
from test_release_060 import FakeCodex, FakeCodexCase

from local_subtitle_tool.core import JobManifest, JobPaths
from local_subtitle_tool.translator import translate


class QuickStallCodex(FakeCodex):
    idle_timeout_seconds = 1.5


class StallTests(FakeCodexCase):
    def setUp(self) -> None:
        super().setUp()
        self._home = patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex-home")})
        self._home.start()

    def tearDown(self) -> None:
        self._home.stop()
        super().tearDown()

    def test_quiet_but_working_request_is_not_stopped(self) -> None:
        # Real Codex prints nothing to stdout while reasoning; only its log grows.
        self.set_plan([{"kind": "quiet_work", "seconds": 4}])
        config = self.job(count=2, batch_size=2)
        with patch(
            "local_subtitle_tool.translator.get_provider",
            side_effect=lambda name: QuickStallCodex(),
        ):
            decisions = translate(config)
        self.assertEqual(len(decisions), 2)
        self.assertEqual(len(self.calls()), 1)

    def test_silent_request_is_stopped_and_resent(self) -> None:
        self.set_plan([{"kind": "sleep"}, {"kind": "ok"}])
        config = self.job(count=2, batch_size=2)
        started = time.monotonic()
        with (
            patch(
                "local_subtitle_tool.translator.get_provider",
                side_effect=lambda name: QuickStallCodex(),
            ),
            patch("local_subtitle_tool.translator.TRANSIENT_BACKOFF_SECONDS", (0.0, 0.0)),
        ):
            decisions = translate(config)
        self.assertLess(time.monotonic() - started, 30)  # not the 60 s sleep
        self.assertEqual(len(decisions), 2)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[1]["resume"])
        events = [
            json.loads(line)
            for line in JobPaths(config).provider_events.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(events[0]["failure_kind"], "TRANSIENT_ERROR")
        self.assertIn("응답하지 않아", events[0]["message"])


class ReviewSpeedReuseTests(ParallelLaneTests):
    def test_review_results_survive_a_speed_change(self) -> None:
        standard = replace(self.job(6, batch_size=6), speed_mode="standard")

        def script(ids, n):
            return compact(ids, review={2} if n == 0 else set())

        first = LaneProvider(script)
        self.run_with(first, standard, parallel_batches=1)
        self.assertEqual(len(first.calls), 2)  # batch + one review request
        fast = replace(standard, speed_mode="fast")
        JobManifest.load_or_create(standard).allow_translation_profile_switch(standard, fast)
        again = LaneProvider(script)
        decisions = self.run_with(again, fast, parallel_batches=1)
        self.assertEqual(again.calls, [])
        self.assertEqual(decisions[2]["ko"], "번역 2")


if __name__ == "__main__":
    unittest.main()


class ReviewDecisionTests(unittest.TestCase):
    def _review_job(self, directory: str):
        import tempfile  # noqa: F401  (kept local; module-level import unused elsewhere)
        from pathlib import Path

        from test_stage1_blockers import ScriptedProvider, _item, _prepare_job

        from local_subtitle_tool.translator import TranslationReviewRequired

        def script(ids, retry, call):
            items = [_item(i, "review", "불명확") if i in (1, 2) else _item(i) for i in ids]
            for item in items:
                if item["raw_id"] == 1:
                    item["ko"] = "아마 이런 뜻"  # tentative translation kept with the review
            return items

        config = _prepare_job(Path(directory), count=4)
        provider = ScriptedProvider(script)
        with (
            patch("local_subtitle_tool.translator.get_provider", return_value=provider),
            self.assertRaises(TranslationReviewRequired),
        ):
            translate(config)
        return config, provider

    def test_accept_and_omit_resolve_review_items(self) -> None:
        import tempfile

        from local_subtitle_tool.translator import (
            accept_model_translations,
            omit_review_items,
            pending_review_items,
        )

        with tempfile.TemporaryDirectory() as directory:
            config, provider = self._review_job(directory)
            accepted, skipped = accept_model_translations(config)
            self.assertEqual((accepted, skipped), (1, [2]))  # 2 has no tentative line
            self.assertEqual(omit_review_items(config, [2]), 1)
            pending = {item["raw_id"]: item for item in pending_review_items(config)}
            self.assertEqual(pending[1]["resolution_label"], "모델 번역 채택")
            self.assertEqual(pending[2]["resolution_label"], "자막에서 삭제함")
            calls = len(provider.calls)
            with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
                decisions = translate(config)
            self.assertEqual(len(provider.calls), calls)
            by_id = {item["raw_id"]: item for item in decisions}
            self.assertEqual(by_id[1]["action"], "translated")
            self.assertEqual(by_id[1]["ko"], "아마 이런 뜻")
            self.assertEqual(by_id[2]["action"], "omitted")


class ReviewGroupStabilityTests(unittest.TestCase):
    def test_resolving_one_line_does_not_resend_other_review_groups(self) -> None:
        import tempfile
        from pathlib import Path

        from test_stage1_blockers import ScriptedProvider, _item, _prepare_job

        from local_subtitle_tool.translator import (
            TranslationReviewRequired,
            accept_model_translations,
        )

        def script(ids, retry, call):
            items = [_item(i, "review", "불명확") if i in (1, 2, 3) else _item(i) for i in ids]
            for item in items:
                if item["action"] == "review":
                    item["ko"] = f"추정 {item['raw_id']}"
            return items

        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory), count=5)
            provider = ScriptedProvider(script)
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                patch("local_subtitle_tool.translator.REVIEW_RETRY_BATCH_SIZE", 2),
                self.assertRaises(TranslationReviewRequired),
            ):
                translate(config)
            calls = len(provider.calls)
            accept_model_translations(config, [1])
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                patch("local_subtitle_tool.translator.REVIEW_RETRY_BATCH_SIZE", 2),
                self.assertRaises(TranslationReviewRequired) as raised,
            ):
                translate(config)
            self.assertEqual(len(provider.calls), calls)  # all served from cache
            self.assertEqual(raised.exception.review_raw_ids, [2, 3])
