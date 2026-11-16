"""PolyCue 0.6.0: persistent Codex sessions, partial repair, streaming metrics.

All tests drive tests/fixtures/fake_codex.py; no real model is ever called.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.core import (
    MANIFEST_SCHEMA_VERSION,
    JobConfig,
    JobManifest,
    JobPaths,
)
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import (
    CodexProvider,
    ProviderCancelled,
    ProviderError,
    ProviderFailureKind,
    terminate_active_processes,
)
from local_subtitle_tool.translator import (
    TranslationPartialError,
    translate,
)

FAKE = Path(__file__).resolve().parent / "fixtures" / "fake_codex.py"


class FakeCodex(CodexProvider):
    executable_override = [sys.executable, str(FAKE)]

    def status(self) -> CheckResult:
        return CheckResult("codex", True, "ok")


class FakeCodexCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.plan = self.root / "plan.json"
        self._env = patch.dict(os.environ, {"FAKE_CODEX_PLAN": str(self.plan)})
        self._env.start()
        self._provider = patch(
            "local_subtitle_tool.translator.get_provider", side_effect=lambda name: FakeCodex()
        )
        self._provider.start()

    def tearDown(self) -> None:
        self._provider.stop()
        self._env.stop()
        self._tmp.cleanup()

    def set_plan(self, steps: list[dict]) -> None:
        self.plan.write_text(json.dumps(steps), encoding="utf-8")
        self.plan.with_suffix(".count").unlink(missing_ok=True)

    def calls(self) -> list[dict]:
        log = self.plan.with_suffix(".log")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def job(self, count: int = 4, batch_size: int = 2, **overrides) -> JobConfig:
        video = self.root / "TEST-060.mp4"
        video.touch()
        config = JobConfig(
            video_path=video,
            code="TEST-060",
            output_dir=self.root / "out",
            batch_size=batch_size,
            reasoning_effort="xhigh",
            speed_mode="fast",
            **overrides,
        )
        paths = JobPaths(config)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        if not paths.raw.exists():
            paths.raw.write_text(
                json.dumps(
                    {
                        "meta": {},
                        "segments": [
                            {
                                "id": i,
                                "start": float(i),
                                "end": i + 0.8,
                                "text": f"発話{i}",
                                "no_speech_prob": 0.0,
                            }
                            for i in range(count)
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return config


class SessionLifecycleTests(FakeCodexCase):
    def test_first_batch_creates_session_and_next_batch_resumes_it(self) -> None:
        self.set_plan([{"kind": "ok"}])
        config = self.job()
        translate(config)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0]["resume"])
        self.assertNotIn("--ephemeral", calls[0]["argv"])
        self.assertEqual(calls[1]["resume"], "thread-0")
        self.assertIn('sandbox_mode="read-only"', calls[1]["argv"])
        self.assertIn('model_reasoning_effort="xhigh"', calls[1]["argv"])
        active = JobManifest.load_or_create(config).translation_sessions["active"]
        self.assertEqual(active["session_id"], "thread-0")
        self.assertEqual(active["turns"], 2)

    def test_saved_session_is_reused_after_restart(self) -> None:
        self.set_plan([{"kind": "ok"}, {"kind": "usage_limit"}, {"kind": "ok"}])
        config = self.job()
        with self.assertRaises(ProviderError):
            translate(config)
        translate(config)  # a new run loads the manifest from disk
        calls = self.calls()
        self.assertEqual(calls[-1]["resume"], "thread-0")
        self.assertEqual(calls[-1]["targets"], [2, 3])

    def test_profile_change_starts_a_new_session(self) -> None:
        self.set_plan([{"kind": "ok"}])
        config = self.job()
        translate(config)
        changed = JobConfig.from_dict({**config.to_dict(), "reasoning_effort": "high"})
        translate(changed)
        calls = self.calls()
        self.assertIsNone(calls[2]["resume"])
        retired = JobManifest.load_or_create(changed).translation_sessions["retired"]
        self.assertEqual(retired[-1]["retired_reason"], "profile_changed")

    def test_disabling_sessions_uses_ephemeral_and_reuses_cache(self) -> None:
        self.set_plan([{"kind": "ok"}])
        config = self.job()
        translate(config, codex_sessions=False)
        calls = self.calls()
        self.assertIn("--ephemeral", calls[0]["argv"])
        self.assertIsNone(calls[1]["resume"])
        translate(config)  # session ids are not part of the batch cache
        self.assertEqual(len(self.calls()), 2)


class RefusalTests(FakeCodexCase):
    def test_refusal_retires_session_and_held_batch_needs_explicit_retry(self) -> None:
        self.set_plan([{"kind": "refusal"}, {"kind": "ok"}])
        config = self.job()
        with self.assertRaises(TranslationPartialError):
            translate(config)
        calls = self.calls()
        self.assertEqual([c["targets"] for c in calls], [[0, 1], [2, 3]])
        self.assertIsNone(calls[1]["resume"])  # new session after the refusal
        sessions = JobManifest.load_or_create(config).translation_sessions
        self.assertEqual(sessions["retired"][0]["retired_reason"], "refused")

        with self.assertRaises(TranslationPartialError):
            translate(config)
        self.assertEqual(len(self.calls()), 2)  # held batch not re-sent automatically

        self.set_plan([{"kind": "ok"}])
        translate(config, retry_held=True)
        retry = self.calls()[-1]
        self.assertEqual(retry["targets"], [0, 1])
        self.assertIsNone(retry["resume"])  # explicit retry uses a fresh session
        reasons = [
            item["retired_reason"]
            for item in JobManifest.load_or_create(config).translation_sessions["retired"]
        ]
        self.assertIn("held_retry", reasons)


class PartialRepairTests(FakeCodexCase):
    def test_only_missing_ids_are_requested_again(self) -> None:
        self.set_plan([{"kind": "ok", "drop": [1]}, {"kind": "ok"}])
        config = self.job(count=3, batch_size=3)
        decisions = translate(config)
        calls = self.calls()
        self.assertEqual([c["targets"] for c in calls], [[0, 1, 2], [1]])
        self.assertEqual(calls[1]["resume"], "thread-0")
        self.assertEqual([d["raw_id"] for d in decisions], [0, 1, 2])
        events = [
            json.loads(line)
            for line in JobPaths(config).provider_events.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([e["stage"] for e in events], ["initial", "repair"])
        self.assertFalse(list(JobPaths(config).batches_dir.glob("partial_*.json")))

    def test_failed_repair_holds_the_batch(self) -> None:
        self.set_plan([{"kind": "ok", "drop": [1]}])  # every repair keeps dropping 1
        config = self.job(count=3, batch_size=3)
        with self.assertRaises(TranslationPartialError):
            translate(config)
        refused = json.loads(JobPaths(config).refused_batches.read_text(encoding="utf-8"))
        self.assertEqual(refused["batches"][0]["failure_kind"], "INVALID_OUTPUT")
        # initial + 2 repairs, then one more attempt resuming from the partial checkpoint
        self.assertTrue(all(c["targets"] in ([0, 1, 2], [1]) for c in self.calls()))


class StreamingAndMetricsTests(FakeCodexCase):
    def _run(self, config: JobConfig) -> None:
        translate(config)

    def test_usage_metrics_and_unavailable_tier(self) -> None:
        usage = {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "cache_write_input_tokens": 0,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
        }
        self.set_plan([{"kind": "ok", "usage": usage}, {"kind": "ok"}])
        config = self.job()
        self._run(config)
        events = [
            json.loads(line)
            for line in JobPaths(config).provider_events.read_text(encoding="utf-8").splitlines()
        ]
        first, second = events[0]["metrics"], events[1]["metrics"]
        self.assertEqual(first["cached_input_tokens"], 40)
        self.assertIsNone(second["input_tokens"])  # not reported -> not guessed
        self.assertEqual(first["observed_service_tier"], "unavailable")
        self.assertEqual(first["requested_service_tier"], "fast")
        self.assertIsNotNone(first["time_to_first_event_seconds"])
        self.assertEqual(first["session_mode"], "new")
        self.assertEqual(second["session_mode"], "resume")
        report_totals = JobManifest.load_or_create(config).stats["translation_metrics"]
        self.assertEqual(report_totals["resumed_requests"], 1)

    def test_resumed_turn_usage_is_converted_to_per_turn(self) -> None:
        first = {
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "cache_write_input_tokens": 0,
        }
        second = {
            "input_tokens": 250,
            "cached_input_tokens": 90,
            "output_tokens": 50,
            "reasoning_output_tokens": 12,
            "cache_write_input_tokens": 0,
        }
        self.set_plan([{"kind": "ok", "usage": first}, {"kind": "ok", "usage": second}])
        config = self.job()
        translate(config)
        events = [
            json.loads(line)
            for line in JobPaths(config).provider_events.read_text(encoding="utf-8").splitlines()
        ]
        resumed = events[1]["metrics"]
        self.assertEqual(resumed["input_tokens"], 150)
        self.assertEqual(resumed["output_tokens"], 30)
        self.assertEqual(resumed["cumulative_input_tokens"], 250)
        totals = JobManifest.load_or_create(config).stats["translation_metrics"]
        self.assertEqual(totals["output_tokens"], 50)

    def test_usage_limit_message_is_short_and_hides_prompt(self) -> None:
        self.set_plan([{"kind": "usage_limit"}])
        config = self.job()
        with self.assertRaises(ProviderError) as raised:
            translate(config)
        self.assertEqual(raised.exception.kind, ProviderFailureKind.USAGE_LIMIT)
        self.assertLessEqual(len(str(raised.exception)), 300)
        self.assertNotIn("입력 데이터", str(raised.exception))
        manifest = JobManifest.load_or_create(config)
        self.assertNotIn("입력 데이터", manifest.error or "")

    def test_stop_kills_running_cli(self) -> None:
        self.set_plan([{"kind": "sleep"}])
        config = self.job()
        paths = JobPaths(config)
        cancel = threading.Event()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                FakeCodex().run(
                    prompt="입력 데이터:\n" + json.dumps({"target_raw_ids": [0]}),
                    model="m",
                    schema_path=paths.state_dir / "schema.json",
                    output_path=paths.state_dir / "out.json",
                    workdir=paths.state_dir,
                    cancel_event=cancel,
                    persistent=True,
                )
            except BaseException as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(1.5)
        cancel.set()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], ProviderCancelled)
        self.assertEqual(terminate_active_processes(), 0)


class SchemaTests(unittest.TestCase):
    def test_schema_four_migrates_to_five(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-061.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="TEST-061", output_dir=root / "out")
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.manifest.write_text(
                json.dumps(
                    {"schema_version": 4, "config": config.to_dict(), "status": "TRANSLATING"}
                ),
                encoding="utf-8",
            )
            manifest = JobManifest.load_or_create(config)
            self.assertEqual(manifest.schema_version, MANIFEST_SCHEMA_VERSION)
            self.assertEqual(
                manifest.translation_sessions, {"active": None, "lanes": {}, "retired": []}
            )


if __name__ == "__main__":
    unittest.main()
