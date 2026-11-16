"""PolyCue 0.7.0: compact translation protocol with quality safeguards."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.core import JobConfig, JobPaths
from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import ProviderResult
from local_subtitle_tool.subtitles import validate_srt_pair
from local_subtitle_tool.translator import (
    TRANSLATION_SCHEMA,
    TranslationPartialError,
    translate,
)


def compact(translations=(), omitted=(), review=(), ja_fixes=(), merges=(), summary=""):
    return {
        "translations": [{"raw_id": i, "ko": ko} for i, ko in translations],
        "ja_fixes": [{"raw_id": i, "ja": ja} for i, ja in ja_fixes],
        "omitted": [{"raw_id": i, "code": code} for i, code in omitted],
        "review": [{"raw_id": i, "ko": ko, "note": note} for i, ko, note in review],
        "merge_with_previous": list(merges),
        "register_summary": summary,
    }


class Scripted:
    """In-process provider: script(ids, payload, call_no) -> compact payload."""

    def __init__(self, script):
        self.script = script
        self.prompts: list[dict] = []

    def status(self) -> CheckResult:
        return CheckResult("fake", True, "ok")

    def run(self, **kwargs) -> ProviderResult:
        payload = json.loads(str(kwargs["prompt"]).split("입력 데이터:\n", 1)[1])
        self.prompts.append(payload)
        result = self.script(payload["target_raw_ids"], payload, len(self.prompts))
        return ProviderResult(payload=result, stdout="", stderr="", elapsed_seconds=0)


class CompactProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def job(
        self, texts: list[str], batch_size: int = 240, extra: dict | None = None
    ) -> JobConfig:
        video = self.root / "TEST-070.mp4"
        video.touch()
        config = JobConfig(
            video_path=video, code="TEST-070", output_dir=self.root / "out", batch_size=batch_size
        )
        paths = JobPaths(config)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        segments = []
        for i, text in enumerate(texts):
            segment = {
                "id": i,
                "start": float(i),
                "end": i + 0.8,
                "text": text,
                "no_speech_prob": 0.0,
                "avg_logprob": -0.2,
            }
            segment.update((extra or {}).get(i, {}))
            segments.append(segment)
        paths.raw.write_text(
            json.dumps({"meta": {}, "segments": segments}, ensure_ascii=False), encoding="utf-8"
        )
        return config

    def run_with(self, provider, config, **kwargs):
        with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
            return translate(config, **kwargs)

    def test_schema_is_compact(self) -> None:
        self.assertIn("translations", TRANSLATION_SCHEMA["properties"])
        self.assertNotIn("items", TRANSLATION_SCHEMA["properties"])

    def test_compact_response_maps_to_internal_decisions(self) -> None:
        config = self.job(["こんにちは", "あぁん…", "大丈夫です", "やめて"])
        provider = Scripted(
            lambda ids, payload, n: compact(
                translations=[(0, "안녕하세요."), (2, "괜찮아요."), (3, "그만해.")],
                omitted=[(1, "moan")],
                ja_fixes=[(2, "大丈夫です。")],
                merges=[3],
                summary="두 사람은 존댓말을 쓴다.",
            )
        )
        decisions = self.run_with(provider, config)
        by_id = {item["raw_id"]: item for item in decisions}
        self.assertEqual(by_id[1]["action"], "omitted")
        self.assertEqual(by_id[1]["reason"], "신음·탄식")
        self.assertEqual(by_id[0]["ja_fixed"], "こんにちは")  # unchanged line keeps raw text
        self.assertEqual(by_id[2]["ja_fixed"], "大丈夫です。")
        self.assertTrue(by_id[3]["merge_with_previous"])
        self.assertEqual(len(provider.prompts), 1)

    def test_suspicious_omission_is_re_requested(self) -> None:
        config = self.job(["こんにちは", "やめて"])

        def script(ids, payload, n):
            if n == 1:
                return compact(translations=[(0, "안녕하세요.")], omitted=[(1, "moan")])
            return compact(translations=[(1, "그만해.")])

        provider = Scripted(script)
        decisions = self.run_with(provider, config)
        self.assertEqual(provider.prompts[1]["target_raw_ids"], [1])
        self.assertEqual(decisions[1]["action"], "translated")
        self.assertEqual(decisions[1]["ko"], "그만해.")

    def test_repetition_loop_may_be_omitted_as_hallucination(self) -> None:
        texts = ["こんにちは"] + ["私は彼女を愛しています。"] * 4 + ["やめて"]
        config = self.job(texts)
        provider = Scripted(
            lambda ids, payload, n: compact(
                translations=[(0, "안녕하세요."), (5, "그만해.")],
                omitted=[(i, "hallucination") for i in range(1, 5)],
            )
        )
        decisions = self.run_with(provider, config)
        self.assertEqual(len(provider.prompts), 1)
        self.assertEqual([d["action"] for d in decisions][1:5], ["omitted"] * 4)

    def test_single_real_line_is_not_omitted_as_hallucination(self) -> None:
        config = self.job(["こんにちは", "愛してる"])

        def script(ids, payload, n):
            if n == 1:
                return compact(translations=[(0, "안녕하세요.")], omitted=[(1, "hallucination")])
            return compact(translations=[(1, "사랑해.")])

        provider = Scripted(script)
        decisions = self.run_with(provider, config)
        self.assertEqual(decisions[1]["ko"], "사랑해.")

    def test_low_confidence_line_requires_japanese_correction(self) -> None:
        config = self.job(["こんにちは", "中途入社"], extra={1: {"avg_logprob": -1.2}})

        def script(ids, payload, n):
            if n == 1:
                return compact(translations=[(0, "안녕하세요."), (1, "경력 입사")])
            return compact(translations=[(1, "경력 입사한 사람")], ja_fixes=[(1, "中途入社の人")])

        provider = Scripted(script)
        decisions = self.run_with(provider, config)
        self.assertEqual(provider.prompts[0]["ja_fix_required_raw_ids"], [1])
        self.assertEqual(provider.prompts[1]["target_raw_ids"], [1])
        self.assertEqual(decisions[1]["ja_fixed"], "中途入社の人")

    def test_kanji_residue_in_ko_is_re_requested(self) -> None:
        config = self.job(["中途入社の人もいるし"])

        def script(ids, payload, n):
            ko = "중途 입사한 사람도 있고" if n == 1 else "경력 입사한 사람도 있고"
            return compact(translations=[(0, ko)])

        provider = Scripted(script)
        decisions = self.run_with(provider, config)
        self.assertEqual(len(provider.prompts), 2)
        self.assertEqual(decisions[0]["ko"], "경력 입사한 사람도 있고")

    def test_register_summary_carries_over_without_changing_cache(self) -> None:
        config = self.job(["はい", "そうです", "行こう", "うん"], batch_size=2)

        def script(ids, payload, n):
            return compact(
                translations=[(i, f"번역 {i}") for i in ids], summary=f"요약 {n}"
            )

        provider = Scripted(script)
        self.run_with(provider, config)
        self.assertNotIn("previous_register_summary", provider.prompts[0])
        self.assertEqual(provider.prompts[1]["previous_register_summary"], "요약 1")
        again = Scripted(script)
        self.run_with(again, config)
        self.assertEqual(again.prompts, [])  # cached batches still valid

    def test_hold_from_older_protocol_is_not_resent(self) -> None:
        config = self.job(["はい", "やめて"], batch_size=2)
        paths = JobPaths(config)
        from local_subtitle_tool.translator import _segment_view

        segments = json.loads(paths.raw.read_text(encoding="utf-8"))["segments"]
        paths.refused_batches.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "batches": [
                        {
                            "batch_index": 0,
                            "fingerprint": "old-protocol-fingerprint",
                            "target_raw_ids": [0, 1],
                            "source_segments": [_segment_view(s) for s in segments],
                            "failure_kind": "CONTENT_REFUSAL",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        provider = Scripted(lambda ids, payload, n: compact(translations=[]))
        with self.assertRaises(TranslationPartialError):
            self.run_with(provider, config)
        self.assertEqual(provider.prompts, [])

    def test_default_batch_size_is_the_even_split_maximum(self) -> None:
        config = JobConfig(video_path=self.root / "x.mp4", code="X-1", output_dir=self.root)
        self.assertEqual(config.batch_size, 240)  # max lines; split evenly across lanes


class ResidueValidationTests(unittest.TestCase):
    def test_kanji_left_in_korean_subtitle_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ja, ko = root / "ja.srt", root / "ko.srt"
            ja.write_text("1\n00:00:00,000 --> 00:00:01,000\n中途入社\n", encoding="utf-8")
            ko.write_text("1\n00:00:00,000 --> 00:00:01,000\n중途 입사\n", encoding="utf-8")
            self.assertEqual(validate_srt_pair(ja, ko).japanese_residue, 1)


if __name__ == "__main__":
    unittest.main()
