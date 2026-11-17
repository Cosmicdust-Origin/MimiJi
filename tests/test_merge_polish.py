from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.core import JobConfig, JobPaths, load_json
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import (
    ProviderError,
    ProviderFailureKind,
    ProviderResult,
)
from local_subtitle_tool.subtitles import build_and_validate
from local_subtitle_tool.translator import translate

POLISHED = (
    "例えば、食事中に俺の彼女が俺の友達のエゴマの葉を剥がしてあげるのは"
    "アリかナシか、って話ですよね？"
)


class QualityProvider:
    supports_quality_gate = True
    supports_sessions = False

    def __init__(self, *, accept: bool = True, refuse_polish: bool = False) -> None:
        self.accept = accept
        self.refuse_polish = refuse_polish
        self.stages: list[str] = []

    def status(self) -> CheckResult:
        return CheckResult("fake", True, "ok")

    def run(self, **kwargs) -> ProviderResult:
        prompt = str(kwargs["prompt"])
        if prompt.startswith("귀하는 병합 자막 교정 결과의 독립적인"):
            self.stages.append("verify")
            payload = {
                "groups": [
                    {
                        "raw_ids": [0, 1, 2],
                        "meaning_preserved": self.accept,
                        "register_preserved": True,
                        "no_omitted_meaning": self.accept,
                        "no_added_meaning": True,
                        "core_action_preserved": True,
                        "boundary_natural": self.accept,
                        "accepted": self.accept,
                        "reason": "" if self.accept else "말머리 의미가 빠졌을 수 있습니다.",
                    }
                ]
            }
        elif prompt.startswith("귀하는 직접 번역이 끝난 병합 자막의"):
            self.stages.append("polish")
            if self.refuse_polish:
                raise ProviderError(
                    "모델이 병합 문장 교정을 처리하지 않았습니다.",
                    kind=ProviderFailureKind.CONTENT_REFUSAL,
                )
            payload = {
                "groups": [
                    {
                        "raw_ids": [0, 1, 2],
                        "changed": True,
                        "polished_target": POLISHED,
                        "note": "raw_id 경계의 말머리와 반복 종결을 정리했습니다.",
                    }
                ]
            }
        else:
            self.stages.append("translate")
            payload = {
                "translations": [
                    {"raw_id": 0, "ja": "俺がさ"},
                    {"raw_id": 1, "ja": "食事中に、たとえば"},
                    {
                        "raw_id": 2,
                        "ja": (
                            "俺の彼女が俺の友達のためにエゴマの葉を剥がしてあげるのが、"
                            "アリかナシかって話。その話ですよね？"
                        ),
                    },
                ],
                "ko_fixes": [],
                "omitted": [],
                "review": [],
                "merge_with_previous": [1, 2],
                "register_summary": "남성 화자는 반말로 설명합니다.",
            }
        return ProviderResult(payload=payload, stdout="", stderr="", elapsed_seconds=0.01)


class MergePolishTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def job(self, name: str = "job") -> JobConfig:
        video = self.root / f"{name}.mp4"
        video.touch()
        config = JobConfig(
            video_path=video,
            code=name,
            output_dir=self.root / f"{name}-out",
            language_pair="ko-ja",
            provider="codex",
            model="gpt-5.6-luna",
            reasoning_effort="xhigh",
            speed_mode="fast",
        )
        paths = JobPaths(config)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        segments = [
            {
                "id": 0,
                "start": 4.69,
                "end": 5.4,
                "text": "내가 저거",
                "no_speech_prob": 0.01,
                "avg_logprob": -0.2,
            },
            {
                "id": 1,
                "start": 5.4,
                "end": 7.2,
                "text": "밥 먹는데 이제 뭐 예를 들면",
                "no_speech_prob": 0.01,
                "avg_logprob": -0.2,
            },
            {
                "id": 2,
                "start": 7.2,
                "end": 15.45,
                "text": (
                    "내 여자친구가 내 친구에게 깻잎을 떼어주는 게 괜찮냐 안 괜찮냐 "
                    "그거 얘기하는 거죠?"
                ),
                "no_speech_prob": 0.01,
                "avg_logprob": -0.2,
            },
        ]
        paths.raw.write_text(
            json.dumps({"meta": {"duration": 20.0}, "segments": segments}, ensure_ascii=False),
            encoding="utf-8",
        )
        return config

    def run_translation(self, config: JobConfig, provider: QualityProvider) -> list[dict]:
        with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
            return translate(config)

    def test_verified_polish_changes_only_final_cue(self) -> None:
        config = self.job("accepted")
        provider = QualityProvider()
        decisions = self.run_translation(config, provider)
        self.assertEqual(provider.stages, ["translate", "polish", "verify"])
        self.assertEqual(decisions[0]["ko"], "俺がさ")
        self.assertTrue(decisions[1]["merge_with_previous"])

        paths = JobPaths(config)
        polish = load_json(paths.merged_cue_polish)
        self.assertEqual(polish["records"][0]["status"], "accepted")
        result = build_and_validate(config)
        self.assertTrue(result.ok)
        target = paths.ko_srt.read_text(encoding="utf-8-sig")
        self.assertIn(POLISHED, target.replace("\n", ""))
        self.assertNotIn("その話ですよね", target)
        report = paths.report.read_text(encoding="utf-8")
        self.assertIn("병합 문장 경계 교정", report)
        self.assertIn("검증 통과·적용 1개", report)
        self.assertIn("gpt-5.6-luna", report)

    def test_rejected_polish_keeps_initial_translation(self) -> None:
        config = self.job("rejected")
        provider = QualityProvider(accept=False)
        self.run_translation(config, provider)
        paths = JobPaths(config)
        self.assertEqual(load_json(paths.merged_cue_polish)["records"][0]["status"], "rejected")
        build_and_validate(config)
        target = paths.ko_srt.read_text(encoding="utf-8-sig")
        self.assertIn("俺がさ食事中に", target.replace("\n", ""))
        self.assertIn("その話ですよね", target)

    def test_refused_polish_is_held_and_not_automatically_retried(self) -> None:
        config = self.job("held")
        first = QualityProvider(refuse_polish=True)
        self.run_translation(config, first)
        paths = JobPaths(config)
        record = load_json(paths.merged_cue_polish)["records"][0]
        self.assertEqual(record["status"], "held")
        self.assertEqual(first.stages, ["translate", "polish"])

        resumed = QualityProvider()
        self.run_translation(config, resumed)
        self.assertEqual(resumed.stages, [])
        build_and_validate(config)
        target = paths.ko_srt.read_text(encoding="utf-8-sig")
        self.assertIn("俺がさ食事中に", target.replace("\n", ""))

    def test_accepted_checkpoint_is_reused(self) -> None:
        config = self.job("cached")
        self.run_translation(config, QualityProvider())
        resumed = QualityProvider()
        self.run_translation(config, resumed)
        self.assertEqual(resumed.stages, [])

    def test_build_ignores_polish_if_raw_id_translation_changed(self) -> None:
        config = self.job("stale")
        self.run_translation(config, QualityProvider())
        paths = JobPaths(config)
        decisions = load_json(paths.decisions)
        decisions["items"][0]["ko"] = "あのさ、"
        paths.decisions.write_text(
            json.dumps(decisions, ensure_ascii=False), encoding="utf-8"
        )

        build_and_validate(config)
        target = paths.ko_srt.read_text(encoding="utf-8-sig")
        self.assertIn("あのさ、食事中に", target.replace("\n", ""))
        self.assertNotIn(POLISHED, target.replace("\n", ""))


if __name__ == "__main__":
    unittest.main()


class ConcurrentQualityProvider:
    """Answers polish and verify requests after a short delay and records overlap."""

    supports_quality_gate = True
    supports_sessions = False

    def __init__(self, delay: float = 0.3) -> None:
        import threading

        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.order: list[str] = []

    def run(self, **kwargs) -> ProviderResult:
        import time

        prompt = str(kwargs["prompt"])
        groups = json.loads(prompt.split("입력 데이터:\n", 1)[1])["groups"]
        verify = prompt.startswith("귀하는 병합 자막 교정 결과의 독립적인")
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.order.append("verify" if verify else "polish")
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
        if verify:
            payload = {
                "groups": [
                    {
                        "raw_ids": group["raw_ids"],
                        "meaning_preserved": True,
                        "register_preserved": True,
                        "no_omitted_meaning": True,
                        "no_added_meaning": True,
                        "core_action_preserved": True,
                        "boundary_natural": True,
                        "accepted": True,
                        "reason": "",
                    }
                    for group in groups
                ]
            }
        else:
            payload = {
                "groups": [
                    {
                        "raw_ids": group["raw_ids"],
                        "changed": True,
                        "polished_target": f"다듬은 문장 {group['raw_ids'][0]}",
                        "note": "",
                    }
                    for group in groups
                ]
            }
        return ProviderResult(payload=payload, stdout="", stderr="", elapsed_seconds=self.delay)


class MergePolishConcurrencyTests(unittest.TestCase):
    def test_chunks_are_polished_and_verified_side_by_side(self) -> None:
        import time

        from local_subtitle_tool.core import JobManifest
        from local_subtitle_tool.translator import MERGE_POLISH_CHUNK_SIZE, _polish_merged_cues

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "long.mp4"
            video.touch()
            config = JobConfig(
                video_path=video, code="long", output_dir=root / "out", language_pair="ja-ko"
            )
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            paths.batches_dir.mkdir(parents=True, exist_ok=True)
            group_count = MERGE_POLISH_CHUNK_SIZE * 2 + 5  # three chunks
            segments, decisions = [], []
            for index in range(group_count * 2):
                start = index * 3.0
                segments.append({"id": index, "start": start, "end": start + 2.0, "text": "それで"})
                decisions.append(
                    {
                        "raw_id": index,
                        "action": "translated",
                        "ja_fixed": "それで",
                        "ko": f"줄 {index}",
                        "merge_with_previous": index % 2 == 1,
                    }
                )
            provider = ConcurrentQualityProvider()
            started = time.monotonic()
            data = _polish_merged_cues(
                provider=provider,
                config=config,
                manifest=JobManifest.load_or_create(config),
                paths=paths,
                segments=segments,
                decisions=decisions,
                register_summary="",
                cancel_event=None,
                progress=None,
            )
            elapsed = time.monotonic() - started

            # An interrupted run leaves polished cues waiting for verification: a rerun
            # only verifies them.
            saved = load_json(paths.merged_cue_polish)
            for record in saved["records"]:
                record["status"] = "verification_pending"
            paths.merged_cue_polish.write_text(
                json.dumps(saved, ensure_ascii=False), encoding="utf-8"
            )
            resumed = ConcurrentQualityProvider(delay=0.01)
            again = _polish_merged_cues(
                provider=resumed,
                config=config,
                manifest=JobManifest.load_or_create(config),
                paths=paths,
                segments=segments,
                decisions=decisions,
                register_summary="",
                cancel_event=None,
                progress=None,
            )
            self.assertEqual(resumed.order, ["verify"] * 3)
            self.assertTrue(all(record["status"] == "accepted" for record in again["records"]))

        records = data["records"]
        self.assertEqual(len(records), group_count)
        self.assertTrue(all(record["status"] == "accepted" for record in records))
        self.assertEqual(provider.order.count("polish"), 3)
        self.assertEqual(provider.order.count("verify"), 3)
        self.assertGreaterEqual(provider.peak, 2)  # requests overlapped
        # one at a time this is 6 x 0.3 s; side by side about 2 x 0.3 s
        self.assertLess(elapsed, 6 * provider.delay * 0.75)
