from __future__ import annotations

import os
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

import local_subtitle_tool
from local_subtitle_tool import appinfo
from local_subtitle_tool.__main__ import build_parser
from local_subtitle_tool.accounts import summarize_account
from local_subtitle_tool.bridge import AppBridge
from local_subtitle_tool.diagnostics import _app_version, app_data_dir
from local_subtitle_tool.jobs import JobRegistry
from local_subtitle_tool.preflight import LOGIN_REQUIRED, MISSING, CheckResult

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_single_semver_source(self) -> None:
        version = local_subtitle_tool.__version__
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(appinfo.APP_VERSION, version)
        self.assertEqual(_app_version(), version)

    def test_pyproject_reads_version_from_version_module(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"]["name"], "mimiji")
        self.assertIn("version", data["project"]["dynamic"])
        self.assertNotIn("version", data["project"])
        self.assertEqual(
            data["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "local_subtitle_tool._version.__version__",
        )

    def test_changelog_top_entry_matches_version(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        releases = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, flags=re.MULTILINE)
        self.assertTrue(releases)
        self.assertEqual(releases[0], local_subtitle_tool.__version__)

    def test_cli_version_flag(self) -> None:
        with self.assertRaises(SystemExit), patch("sys.stdout") as stdout:
            build_parser().parse_args(["--version"])
        written = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn(f"MimiJi {local_subtitle_tool.__version__}", written)


class DataMigrationTests(unittest.TestCase):
    def test_polycue_data_is_preferred_over_older_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            for name, marker in (("PolyCue", "polycue"), ("LocalSubtitleTool", "legacy")):
                folder = Path(directory) / name
                folder.mkdir()
                (folder / "jobs.json").write_text(marker, encoding="utf-8")
            target = app_data_dir()
            self.assertEqual(target.name, "MimiJi")
            self.assertEqual((target / "jobs.json").read_text(encoding="utf-8"), "polycue")

    def test_legacy_job_list_is_copied_once_into_polycue_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"LOCALAPPDATA": directory}
        ):
            legacy = Path(directory) / "LocalSubtitleTool"
            legacy.mkdir()
            (legacy / "jobs.json").write_text('{"schema_version": 1, "jobs": []}', "utf-8")
            target = app_data_dir()
            self.assertEqual(target.name, "MimiJi")
            self.assertTrue((target / "jobs.json").is_file())
            self.assertTrue((legacy / "jobs.json").is_file())  # original kept
            (target / "jobs.json").write_text("changed", encoding="utf-8")
            app_data_dir()
            self.assertEqual((target / "jobs.json").read_text(encoding="utf-8"), "changed")


class AccountSummaryTests(unittest.TestCase):
    def test_states(self) -> None:
        claude = summarize_account(
            "claude",
            CheckResult(
                "claude",
                True,
                "ok",
                {"authMethod": "claude.ai", "subscriptionType": "max", "version": "2.1"},
            ),
        )
        self.assertTrue(claude["logged_in"])
        self.assertIn("max", claude["detail"])
        missing = summarize_account("codex", CheckResult("codex", False, "없음", status=MISSING))
        self.assertFalse(missing["installed"])
        self.assertEqual(missing["state"], "설치 필요")
        login = summarize_account(
            "codex", CheckResult("codex", False, "필요", status=LOGIN_REQUIRED)
        )
        self.assertTrue(login["installed"])
        self.assertEqual(login["state"], "로그인 필요")


class FakeProvider:
    def __init__(self, logged_in: bool) -> None:
        self.logged_in = logged_in
        self.logged_out = False

    def status(self) -> CheckResult:
        if self.logged_in:
            return CheckResult("codex", True, "ok", {"login": "Logged in using ChatGPT"})
        return CheckResult("codex", False, "로그인 필요", status=LOGIN_REQUIRED)

    def logout(self) -> None:
        self.logged_out = True
        self.logged_in = False


class BridgeAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QQuickStyle.setStyle("Basic")
        cls.app = QApplication.instance() or QApplication([])

    def test_logout_updates_account_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = AppBridge(
                settings=QSettings(str(root / "s.ini"), QSettings.Format.IniFormat),
                registry=JobRegistry(root / "registry"),
                auto_diagnose=False,
            )
            provider = FakeProvider(logged_in=True)
            results: list[tuple] = []

            def launch(operation, target):
                results.append((operation, target()))

            with (
                patch("local_subtitle_tool.bridge.get_provider", return_value=provider),
                patch.object(bridge, "_launch", side_effect=launch),
            ):
                bridge.refreshAccounts()
                operation, result = results[-1]
                bridge._finish_operation(operation, result, None)
                self.assertTrue(bridge.providerAccountReady)
                self.assertIn("로그인됨", bridge.providerAccountText)

                bridge.logoutProvider("codex")
                operation, result = results[-1]
                bridge._finish_operation(operation, result, None)
            self.assertTrue(provider.logged_out)
            self.assertFalse(bridge.providerAccountReady)
            self.assertEqual(bridge.appName, "MimiJi")
            self.assertEqual(bridge.appVersion, local_subtitle_tool.__version__)


class ThemeSettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QQuickStyle.setStyle("Basic")
        cls.app = QApplication.instance() or QApplication([])

    def test_dark_mode_is_remembered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ini = str(root / "s.ini")

            def make() -> AppBridge:
                return AppBridge(
                    settings=QSettings(ini, QSettings.Format.IniFormat),
                    registry=JobRegistry(root / "registry"),
                    auto_diagnose=False,
                )

            first = make()
            self.assertTrue(first.darkMode)  # dark is the default
            first.darkMode = False
            self.assertFalse(make().darkMode)


if __name__ == "__main__":
    unittest.main()
