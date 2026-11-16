# ruff: noqa: E501
"""0.9.3: natural Japanese line breaks; rescue pass ignores echoed term hints."""

import unittest

from local_subtitle_tool.subtitles import _wrap_japanese


class JapaneseWrapTests(unittest.TestCase):
    def test_break_after_particle_not_inside_a_word(self) -> None:
        text = "食事中に、例えば、彼女が俺の友達にエゴマの葉を取ってあげるのがアリかナシか、って話ですよね？"
        head, tail = _wrap_japanese(text).split("\n")
        self.assertTrue(head.endswith("葉を"))
        self.assertTrue(tail.startswith("取って"))

    def test_break_prefers_punctuation_near_the_middle(self) -> None:
        text = "今日はとても楽しかったですね、また一緒に遊びに行きましょうね本当に"
        self.assertEqual(_wrap_japanese(text).split("\n")[0], "今日はとても楽しかったですね、")

    def test_short_line_and_final_punctuation_stay_single(self) -> None:
        self.assertEqual(_wrap_japanese("もちろん、そう思います。"), "もちろん、そう思います。")
        self.assertNotIn("\n\n", _wrap_japanese("あ" * 40 + "。"))

    def test_lines_stay_within_limit_when_possible(self) -> None:
        text = "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめも"
        self.assertTrue(all(len(line) <= 28 for line in _wrap_japanese(text).split("\n")))


if __name__ == "__main__":
    unittest.main()


class RescueTermEchoTests(unittest.TestCase):
    def test_line_made_only_of_term_hints_is_dropped(self) -> None:
        from local_subtitle_tool.transcriber import _rescue_reject_reason

        terms = ["소지섭", "서수민", "문쌤"]
        echo = {"text": "서수민, 문쌤", "start": 60.0, "end": 60.8,
                "no_speech_prob": 0.03, "avg_logprob": -0.07, "compression_ratio": 1.0}
        real = {**echo, "text": "진짜 뭔가 문쌤 뱃살처럼 생기지 않았나요", "start": 69.4, "end": 73.2}
        self.assertEqual(_rescue_reject_reason(echo, [], terms), "term_echo")
        self.assertIsNone(_rescue_reject_reason(real, [], terms))
        self.assertIsNone(_rescue_reject_reason(echo, []))  # no hints given: not an echo


class RescueDuplicateTests(unittest.TestCase):
    def _seg(self, text: str, start: float, end: float) -> dict:
        return {"text": text, "start": start, "end": end, "no_speech_prob": 0.1,
                "avg_logprob": -0.3, "compression_ratio": 1.0}

    def test_same_line_with_an_extra_word_nearby_is_a_duplicate(self) -> None:
        from local_subtitle_tool.transcriber import _rescue_reject_reason

        kept = [self._seg("어디 가셨지?", 96.2, 97.0)]
        self.assertEqual(_rescue_reject_reason(self._seg("쌤이 어디 가셨지?", 95.0, 96.4), kept), "duplicate")
        # far away, the same words are a new line
        self.assertIsNone(_rescue_reject_reason(self._seg("쌤이 어디 가셨지?", 120.0, 121.0), kept))

    def test_gap_is_measured_against_written_lines_not_vad(self) -> None:
        import numpy as np

        from local_subtitle_tool.transcriber import ClipWindow, _rescue_spans

        audio = (np.ones(16000 * 90, dtype=np.float32) * 0.1)  # loud everywhere
        window = ClipWindow(index=1, start=30.0, end=60.0)
        written = [self._seg("앞 창에서 넘어온 줄", 29.0, 31.0), self._seg("우삼겹 덮밥 먹었어", 31.0, 53.5)]
        spans = _rescue_spans(audio, window, written)
        self.assertEqual(len(spans), 1)
        self.assertAlmostEqual(spans[0].start, 53.0)
        self.assertEqual(spans[0].end, 60.0)

    def test_long_line_repeating_two_short_lines_is_overlap(self) -> None:
        from local_subtitle_tool.transcriber import _rescue_reject_reason

        kept = [self._seg("야이 씨", 76.49, 77.23), self._seg("야 이거 짜줄 사람? 내가 만원 줄게", 78.35, 80.11)]
        long = self._seg("야 이거 짜줄 사람? 내가 많아줄게. 이거 하나만 짜줄 사람?", 76.4, 81.5)
        self.assertEqual(_rescue_reject_reason(long, kept), "overlap")
        quiet = self._seg("진짜 뭔가 뱃살처럼 생기지 않았나요 여러분? 그쵸?", 69.36, 74.18)
        self.assertIsNone(_rescue_reject_reason(quiet, [self._seg("승승패스", 71.05, 71.65)]))
