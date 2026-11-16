"""MimiJi 0.9.2: Korean / Japanese screen language."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from local_subtitle_tool.i18n import translate


class TranslateTests(unittest.TestCase):
    def test_exact_patterns_and_fallback(self) -> None:
        self.assertEqual(translate("새 작업", "ja"), "新しい作業")
        self.assertEqual(translate("새 작업", "ko"), "새 작업")
        self.assertEqual(
            translate("번역 배치 3/3 처리 중 · raw_id 122~182", "ja"),
            "翻訳バッチ 3/3 を処理中 · raw_id 122〜182",
        )
        self.assertEqual(
            translate("Codex · ChatGPT · 로그인됨", "ja"), "Codex · ChatGPT · ログイン済み"
        )
        self.assertEqual(translate("i7 · 20스레드 · AVX2", "ja"), "i7 · 20 スレッド · AVX2")
        self.assertEqual(translate("번역 안 된 문장", "ja"), "번역 안 된 문장")

    def test_every_qml_string_has_japanese(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "local_subtitle_tool" / "qml"
        missing = []
        for path in root.rglob("*.qml"):
            for match in re.finditer(r'"((?:[^"\\n]|\.)*)"', path.read_text(encoding="utf-8")):
                value = match.group(1).replace("\n", "\n")
                if re.search("[가-힣]", value) and value not in {"한국어"}:
                    if translate(value, "ja") == value:
                        missing.append((path.name, value))
        self.assertEqual(missing, [])


class BridgeLanguageTests(unittest.TestCase):
    def test_switching_language_translates_bridge_texts(self) -> None:
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        from local_subtitle_tool.bridge import AppBridge
        from local_subtitle_tool.jobs import JobRegistry

        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = QSettings(str(root / "s.ini"), QSettings.Format.IniFormat)
            bridge = AppBridge(
                settings=settings, registry=JobRegistry(root / "r"), auto_diagnose=False
            )
            self.assertEqual(bridge.uiLanguage, "ko")
            self.assertEqual(bridge.taskTitle, "대기 중")
            received: list[str] = []
            bridge.notification.connect(received.append)
            bridge.uiLanguage = "ja"
            self.assertEqual(bridge.taskTitle, "待機中")
            self.assertIn("日本語 → 韓国語", bridge.languagePairLabels)
            bridge._notify("설정을 저장했습니다.")
            self.assertEqual(received[-1], "設定を保存しました。")
            self.assertEqual(settings.value("ui/language"), "ja")
            again = AppBridge(
                settings=settings, registry=JobRegistry(root / "r"), auto_diagnose=False
            )
            self.assertEqual(again.uiLanguage, "ja")  # remembered


if __name__ == "__main__":
    unittest.main()
