"""MimiJi 0.9.1: Korean audio to Japanese subtitles (language direction ko-ja)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_parallel_lanes import LaneProvider

from local_subtitle_tool.core import JobConfig, JobPaths
from local_subtitle_tool.subtitles import build_and_validate
from local_subtitle_tool.translator import (
    TRANSLATION_SCHEMA,
    TRANSLATION_SCHEMA_KO_JA,
    _batch_fingerprint,
    translate,
)


def ko_ja(ids, review=(), bad=()):
    return {
        "translations": [
            {"raw_id": i, "ja": "안녕" if i in bad else f"翻訳{i}"} for i in ids if i not in review
        ],
        "ko_fixes": [],
        "omitted": [],
        "review": [{"raw_id": i, "ja": f"推定{i}", "note": "不確実"} for i in review],
        "merge_with_previous": [],
        "register_summary": "두 사람은 반말을 쓴다.",
    }


class KoreanToJapaneseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def job(self, count: int = 4) -> JobConfig:
        video = self.root / "movie.mkv"
        video.touch()
        config = JobConfig(
            video_path=video,
            code="광해 테스트",
            output_dir=self.root / "out",
            language_pair="ko-ja",
        )
        paths = JobPaths(config)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        segments = [
            {"id": i, "start": float(i * 2), "end": i * 2 + 1.5, "text": f"대사 {i}입니다"}
            for i in range(count)
        ]
        paths.raw.write_text(
            json.dumps({"meta": {"duration": 60.0}, "segments": segments}, ensure_ascii=False),
            encoding="utf-8",
        )
        return config

    def run_with(self, provider, config):
        with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
            return translate(config, parallel_batches=1)

    def test_schema_and_prompt_are_mirrored(self) -> None:
        props = TRANSLATION_SCHEMA_KO_JA["properties"]
        self.assertIn("ko_fixes", props)
        self.assertNotIn("ja_fixes", props)
        self.assertIn("ja", props["translations"]["items"]["properties"])
        self.assertIn("ko", props["ko_fixes"]["items"]["properties"])
        self.assertIn("ja_fixes", TRANSLATION_SCHEMA["properties"])  # ja-ko unchanged

        prompts: list[str] = []

        class Capture(LaneProvider):
            def run(self, **kwargs):
                prompts.append(str(kwargs["prompt"]))
                return super().run(**kwargs)

        config = self.job()
        self.run_with(Capture(lambda ids, n: ko_ja(ids)), config)
        self.assertIn("한국어 음성 인식 결과를 보정하고 일본어 자막으로", prompts[0])
        self.assertIn("ko_fix_required_raw_ids", prompts[0])

    def test_translation_and_subtitles(self) -> None:
        config = self.job()
        decisions = self.run_with(LaneProvider(lambda ids, n: ko_ja(ids)), config)
        self.assertEqual([item["ko"] for item in decisions], [f"翻訳{i}" for i in range(4)])
        self.assertEqual(decisions[0]["ja_fixed"], "대사 0입니다")  # source kept as Korean
        result = build_and_validate(config)
        self.assertTrue(result.ok, result.to_dict())
        paths = JobPaths(config)
        self.assertTrue(paths.ko_srt.name.endswith("_final_ja.srt"))
        self.assertTrue(paths.ja_srt.name.endswith("_resegmented_ko.srt"))
        self.assertIn("翻訳1", paths.ko_srt.read_text(encoding="utf-8-sig"))
        self.assertIn("대사 1입니다", paths.ja_srt.read_text(encoding="utf-8-sig"))

    def test_hangul_left_in_japanese_is_re_requested(self) -> None:
        config = self.job(2)

        def script(ids, n):
            return ko_ja(ids, bad={1} if n == 0 else set())

        provider = LaneProvider(script)
        decisions = self.run_with(provider, config)
        self.assertEqual(provider.calls[1]["targets"], [1])
        self.assertEqual(decisions[1]["ko"], "翻訳1")

    def test_direction_is_part_of_the_fingerprint(self) -> None:
        ja_ko = JobConfig(video_path=self.root / "a.mp4", code="A", output_dir=self.root)
        ko_ja_config = JobConfig(
            video_path=self.root / "a.mp4", code="A", output_dir=self.root, language_pair="ko-ja"
        )
        self.assertNotEqual(
            _batch_fingerprint(ja_ko, prompt="p", target_ids=[1]),
            _batch_fingerprint(ko_ja_config, prompt="p", target_ids=[1]),
        )
        self.assertNotIn("language_pair", ja_ko.to_dict())  # older jobs stay identical
        self.assertEqual(JobConfig.from_dict(ko_ja_config.to_dict()).language_pair, "ko-ja")


if __name__ == "__main__":
    unittest.main()


class JapaneseWrapTests(unittest.TestCase):
    def test_no_empty_second_line(self) -> None:
        from local_subtitle_tool.subtitles import _wrap_japanese

        text = "無礼な！よくも殿下を偽物呼ばわりを。何たる暴言か。"
        wrapped = _wrap_japanese(text)
        self.assertTrue(all(line for line in wrapped.split("\n")), repr(wrapped))
        long_text = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほ。"
        self.assertTrue(all(line for line in _wrap_japanese(long_text).split("\n")))
