# ruff: noqa: E501
"""0.9.3: Japanese line breaks, rescue-pass filters, Japanese screen coverage."""

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


class JapaneseScreenCoverageTests(unittest.TestCase):
    """In 日本語 mode no app-written Korean may reach the screen."""

    HANGUL = __import__("re").compile("[가-힣]")

    def test_bridge_properties_have_no_korean_in_japanese_mode(self) -> None:
        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        from local_subtitle_tool.bridge import _UNTRANSLATED_KEYS, AppBridge
        from local_subtitle_tool.jobs import JobRegistry

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = QSettings(str(root / "s.ini"), QSettings.Format.IniFormat)
            settings.setValue("ui/language", "ja")
            bridge = AppBridge(settings=settings, registry=JobRegistry(root / "r"), auto_diagnose=False)
            meta = bridge.metaObject()
            names = [meta.property(i).name() for i in range(meta.propertyOffset(), meta.propertyCount())]
            found: set[tuple[str, str]] = set()

            def walk(name: str, value: object, key: str | None = None) -> None:
                if isinstance(value, str):
                    if key not in _UNTRANSLATED_KEYS and self.HANGUL.search(value):
                        found.add((name, value))
                elif isinstance(value, dict):
                    for item_key, item in value.items():
                        walk(name, item, item_key)
                elif isinstance(value, (list, tuple)):
                    for item in value:
                        walk(name, item, key)

            for pair in ("ja-ko", "ko-ja"):
                bridge.languagePair = pair
                for provider in ("claude", "codex"):
                    bridge.provider = provider
                    for name in names:
                        walk(name, getattr(bridge, name))
            self.assertEqual(sorted(found), [])
            self.assertEqual(bridge.fileName, "動画が選択されていません。")

    def test_held_reason_is_translated_but_model_note_is_not(self) -> None:
        import tempfile
        from pathlib import Path

        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        from local_subtitle_tool.bridge import AppBridge
        from local_subtitle_tool.jobs import JobRegistry

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = QSettings(str(root / "s.ini"), QSettings.Format.IniFormat)
            settings.setValue("ui/language", "ja")
            bridge = AppBridge(settings=settings, registry=JobRegistry(root / "r"), auto_diagnose=False)
            bridge._review_items = [
                {"raw_id": 1, "kind": "held", "reason": "모델이 번역 배치 2을 거절했습니다. 모델 응답: “No”"},
                {"raw_id": 2, "kind": "review", "reason": "모델 메모는 그대로"},
            ]
            held, review = bridge.reviewItems
            self.assertEqual(held["reason"], "モデルが翻訳バッチ 2 を拒否しました。モデルの応答:「No」")
            self.assertEqual(review["reason"], "모델 메모는 그대로")

    def test_screen_messages_from_python_modules_have_japanese(self) -> None:
        import ast
        from pathlib import Path

        from local_subtitle_tool.i18n import translate

        package = Path(__file__).resolve().parents[1] / "src" / "local_subtitle_tool"
        missing = []
        # Modules whose messages reach the log, notifications, error lines or dialogs.
        # (translator.py is mostly model instructions; its screen messages are checked below.)
        for name in ("bridge.py", "jobs.py", "diagnostics.py", "transcriber.py", "diarization.py"):
            tree = ast.parse((package / name).read_text(encoding="utf-8"))
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

            def is_developer_only(node: ast.AST, parents: dict = parents) -> bool:
                parent = parents.get(node)
                if isinstance(parent, ast.Expr):  # docstring
                    return True
                while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.Module)):
                    if (
                        isinstance(parent, ast.Call)
                        and isinstance(parent.func, ast.Attribute)
                        and isinstance(parent.func.value, ast.Name)
                        and parent.func.value.id == "_logger"
                    ):
                        return True
                    parent = parents.get(parent)
                return False

            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    text = "".join(v.value if isinstance(v, ast.Constant) else "7" for v in node.values)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if isinstance(parents.get(node), ast.JoinedStr):
                        continue
                    text = node.value + ("7" if node.value.endswith(": ") else "")
                else:
                    continue
                if len(text) < 2 or not self.HANGUL.search(text) or is_developer_only(node):
                    continue
                if self.HANGUL.search(translate(text, "ja")):
                    missing.append(f"{name}:{node.lineno} {text[:60]}")
        self.assertEqual(missing, [])

    def test_composed_translation_messages(self) -> None:
        from local_subtitle_tool.i18n import translate

        cases = {
            "모델이 번역 배치 3을 거절했습니다 · 2회째. 모델 응답: “I can't”":
                "モデルが翻訳バッチ 3 を拒否しました（2 回目）。モデルの応答:「I can't」",
            "번역 배치 2이 모델에 의해 거절되어 보류했습니다. 다음 배치를 새 세션으로 계속합니다.":
                "翻訳バッチ 2 はモデルに拒否されたため保留しました。次のバッチは新しいセッションで続けます。",
            "1차 보강 후에도 확정되지 않은 3개를 한 줄씩 더 넓은 문맥으로 다시 확인합니다.":
                "1 回目の補強でも確定しなかった 3 件を、さらに広い文脈で 1 行ずつ確認し直します。",
            "속도 모드 값은 standard, fast 중 하나여야 합니다.": "処理速度 の値は standard, fast のいずれかにしてください。",
            "최종 자막을 만들 수 없습니다. 미매핑 2개, 검토 필요 5개입니다.":
                "最終字幕を作成できません。未対応 2 件、要確認 5 件があります。",
            "병합 문장 교정 완료: 적용 4개, 수정 불필요 9개, 초안 유지 1개.":
                "結合字幕の整え直しが完了: 適用 4 件、修正不要 9 件、元の訳を維持 1 件。",
        }
        for korean, japanese in cases.items():
            self.assertEqual(translate(korean, "ja"), japanese)
            self.assertEqual(translate(korean, "ko"), korean)
