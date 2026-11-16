"""PolyCue 0.7.1: translation batches and review groups run in parallel lanes."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import ProviderError, ProviderFailureKind, ProviderResult
from local_subtitle_tool.translator import translate


def compact(ids, review=()):
    return {
        "translations": [{"raw_id": i, "ko": f"번역 {i}"} for i in ids if i not in review],
        "ja_fixes": [],
        "omitted": [],
        "review": [{"raw_id": i, "ko": f"검토 {i}", "note": "불확실"} for i in review],
        "merge_with_previous": [],
        "register_summary": "요약",
    }


class LaneProvider:
    """Thread-safe in-process provider with per-call session ids."""

    supports_sessions = True

    def __init__(self, script, barrier: threading.Barrier | None = None) -> None:
        self.script = script
        self.barrier = barrier
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def status(self) -> CheckResult:
        return CheckResult("fake", True, "ok")

    def run(self, **kwargs) -> ProviderResult:
        payload = json.loads(str(kwargs["prompt"]).split("입력 데이터:\n", 1)[1])
        with self.lock:
            number = len(self.calls)
            self.calls.append(
                {
                    "targets": payload["target_raw_ids"],
                    "resume": kwargs.get("session_id"),
                    "summary": payload.get("previous_register_summary"),
                    "thread": threading.current_thread().name,
                }
            )
        if self.barrier is not None:
            self.barrier.wait(timeout=5)  # proves two requests are in flight at once
        result = self.script(payload["target_raw_ids"], number)
        session = kwargs.get("session_id") or f"thread-{number}"
        return ProviderResult(
            payload=result,
            stdout="",
            stderr="",
            elapsed_seconds=0,
            session_id=session,
            session_mode="resume" if kwargs.get("session_id") else "new",
        )


class ParallelLaneTests(unittest.TestCase):
    # These tests exercise several batches per lane; lanes do not grow with the work.
    lane_limit = 2

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._lanes = patch(
            "local_subtitle_tool.translator.MAX_TRANSLATION_LANES", self.lane_limit
        )
        self._lanes.start()

    def tearDown(self) -> None:
        self._lanes.stop()
        self._tmp.cleanup()

    def job(self, count: int, batch_size: int) -> JobConfig:
        video = self.root / "TEST-071.mp4"
        video.touch()
        config = JobConfig(
            video_path=video, code="TEST-071", output_dir=self.root / "out", batch_size=batch_size
        )
        paths = JobPaths(config)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        segments = [
            {"id": i, "start": float(i), "end": i + 0.8, "text": f"発話{i}", "no_speech_prob": 0.0}
            for i in range(count)
        ]
        paths.raw.write_text(
            json.dumps({"meta": {}, "segments": segments}, ensure_ascii=False), encoding="utf-8"
        )
        return config

    def run_with(self, provider, config, **kwargs):
        with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
            return translate(config, **kwargs)

    def test_batches_run_concurrently_in_contiguous_lanes(self) -> None:
        config = self.job(8, batch_size=2)
        provider = LaneProvider(lambda ids, n: compact(ids), barrier=threading.Barrier(2))
        decisions = self.run_with(provider, config, parallel_batches=2)
        self.assertEqual([item["ko"] for item in decisions], [f"번역 {i}" for i in range(8)])
        by_targets = {tuple(call["targets"]): call for call in provider.calls}
        # Lane 0 = batches 0-1, lane 1 = batches 2-3; each lane resumes its own session.
        self.assertIsNone(by_targets[(0, 1)]["resume"])
        self.assertIsNone(by_targets[(4, 5)]["resume"])
        self.assertIsNotNone(by_targets[(2, 3)]["resume"])
        self.assertEqual(by_targets[(2, 3)]["resume"], by_targets[(0, 1)]["resume"] or "thread-0")
        self.assertNotEqual(by_targets[(2, 3)]["resume"], by_targets[(6, 7)]["resume"])
        self.assertEqual(by_targets[(6, 7)]["summary"], "요약")  # carried within the lane
        self.assertIsNone(by_targets[(4, 5)]["summary"])
        sessions = JobManifest.load_or_create(config).translation_sessions
        self.assertIsNotNone(sessions["active"])
        self.assertIn("1", sessions["lanes"])
        self.assertNotEqual(sessions["active"]["session_id"], sessions["lanes"]["1"]["session_id"])

    def test_cached_parallel_run_is_reused_by_sequential_run(self) -> None:
        config = self.job(6, batch_size=2)
        self.run_with(LaneProvider(lambda ids, n: compact(ids)), config, parallel_batches=2)
        again = LaneProvider(lambda ids, n: compact(ids))
        self.run_with(again, config, parallel_batches=1)
        self.assertEqual(again.calls, [])

    def test_failure_in_one_lane_stops_the_run(self) -> None:
        config = self.job(8, batch_size=2)

        def script(ids, n):
            if ids == [4, 5]:
                raise ProviderError("limit", kind=ProviderFailureKind.USAGE_LIMIT)
            return compact(ids)

        provider = LaneProvider(script)
        with self.assertRaises(ProviderError) as caught:
            self.run_with(provider, config, parallel_batches=2)
        self.assertEqual(caught.exception.kind, ProviderFailureKind.USAGE_LIMIT)
        self.assertNotIn([6, 7], [call["targets"] for call in provider.calls])
        # Finished batches are kept; the next run only sends what is left.
        retry = LaneProvider(lambda ids, n: compact(ids))
        self.run_with(retry, config, parallel_batches=2)
        sent = sorted(i for call in retry.calls for i in call["targets"])
        self.assertTrue(set(sent) <= {2, 3, 4, 5, 6, 7})
        self.assertIn(4, sent)

    def test_review_groups_are_recovered_in_parallel(self) -> None:
        config = self.job(40, batch_size=40)
        review_ids = {3, 30}
        both_in_flight = threading.Barrier(2)
        seen: set[int] = set()
        lock = threading.Lock()

        def script(ids, n):
            with lock:
                first = [i for i in ids if i in review_ids and i not in seen]
                seen.update(ids)
            if len(ids) > 1:  # first-pass batches
                return compact(ids, review=set(first))
            both_in_flight.wait(timeout=5)  # review re-requests run concurrently
            return compact(ids)

        provider = LaneProvider(script)
        with patch("local_subtitle_tool.translator.REVIEW_RETRY_BATCH_SIZE", 1):
            decisions = self.run_with(provider, config, parallel_batches=2)
        by_id = {item["raw_id"]: item for item in decisions}
        self.assertEqual(by_id[3]["ko"], "번역 3")
        self.assertEqual(by_id[30]["ko"], "번역 30")
        review_calls = [call for call in provider.calls if len(call["targets"]) == 1]
        self.assertEqual(sorted(call["targets"] for call in review_calls), [[3], [30]])
        self.assertEqual(len({call["thread"] for call in review_calls}), 2)


class ProgressTests(ParallelLaneTests):
    def test_progress_never_drops_and_review_rounds_are_explained(self) -> None:
        config = self.job(40, batch_size=10)

        def script(ids, n):
            review = {3} if 3 in ids and n < 4 else set()
            return compact(ids, review=review)

        values: list[tuple[float, str]] = []
        provider = LaneProvider(script)
        with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
            translate(
                config,
                parallel_batches=2,
                progress_value=lambda fraction, message: values.append((fraction, message)),
            )
        fractions = [value for value, _ in values]
        self.assertEqual(fractions, sorted(fractions))
        messages = " | ".join(message for _, message in values)
        self.assertIn("검토 보강 1차(묶음)", messages)
        self.assertEqual(fractions[-1], 1.0)


if __name__ == "__main__":
    unittest.main()
