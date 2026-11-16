from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.preflight import (
    ERROR,
    CheckResult,
    claude_status,
    codex_status,
    diagnose,
    first_blocker,
    prepare_command,
    required_checks_ready,
)


class AuthenticationStatusTests(unittest.TestCase):
    @patch("local_subtitle_tool.preflight.shutil.which", return_value="codex.exe")
    @patch("local_subtitle_tool.preflight.run_command")
    def test_codex_requires_chatgpt_login(self, run_command, _which) -> None:
        run_command.return_value = subprocess.CompletedProcess(
            ["codex"], 0, stdout="Logged in using an API key", stderr=""
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(codex_status().ok)
        run_command.return_value = subprocess.CompletedProcess(
            ["codex"], 0, stdout="Logged in using ChatGPT", stderr=""
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(codex_status().ok)

    @patch("local_subtitle_tool.preflight.shutil.which", return_value="claude.exe")
    @patch("local_subtitle_tool.preflight.run_command")
    def test_claude_requires_claude_ai_login(self, run_command, _which) -> None:
        console = {"loggedIn": True, "authMethod": "console", "subscriptionType": "api"}
        run_command.return_value = subprocess.CompletedProcess(
            ["claude"], 0, stdout=json.dumps(console), stderr=""
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(claude_status().ok)
        subscription = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "subscriptionType": "pro",
        }
        run_command.return_value = subprocess.CompletedProcess(
            ["claude"], 0, stdout=json.dumps(subscription), stderr=""
        )
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(claude_status().ok)


class DiagnosisResilienceTests(unittest.TestCase):
    def test_windows_cmd_is_wrapped_with_command_processor(self) -> None:
        command = prepare_command(Path(r"C:\Users\test\codex.cmd"), "login", "status")
        if os.name == "nt":
            self.assertEqual(command[1:4], ["/d", "/s", "/c"])
            self.assertEqual(command[4], r"C:\Users\test\codex.cmd")
        else:
            self.assertEqual(command[0], r"C:\Users\test\codex.cmd")

    @patch("local_subtitle_tool.preflight.claude_status")
    @patch("local_subtitle_tool.preflight.codex_status")
    @patch("local_subtitle_tool.preflight.check_cuda")
    @patch("local_subtitle_tool.preflight.check_python_dependencies")
    @patch("local_subtitle_tool.preflight.check_ffprobe")
    def test_one_failure_does_not_stop_other_checks(
        self,
        ffprobe,
        python_dependencies,
        cuda,
        codex,
        claude,
    ) -> None:
        ffprobe.side_effect = FileNotFoundError("broken executable")
        python_dependencies.return_value = CheckResult("python_dependencies", True, "ok")
        cuda.return_value = CheckResult("cuda", True, "ok")
        codex.return_value = CheckResult("codex", True, "ok", required=False)
        claude.return_value = CheckResult("claude", True, "ok", required=False)
        results = diagnose(None)
        self.assertEqual(len(results), 5)
        self.assertEqual(results[0].name, "ffprobe")
        self.assertEqual(results[0].status, ERROR)
        self.assertTrue(results[-1].ok)

    def test_priority_and_selected_provider_gate_next_step(self) -> None:
        results = [
            CheckResult("media", True, "ok", priority=10),
            CheckResult("ffprobe", True, "ok", priority=20),
            CheckResult("python_dependencies", False, "missing", priority=30),
            CheckResult("cuda", False, "blocked", priority=40),
            CheckResult("codex", True, "ok", priority=50, required=False),
            CheckResult("claude", False, "login", priority=60, required=False),
        ]
        self.assertFalse(required_checks_ready(results, "codex"))
        self.assertEqual(first_blocker(results, "codex").name, "python_dependencies")
        results[2].ok = True
        results[3].ok = True
        self.assertTrue(required_checks_ready(results, "codex"))
        self.assertFalse(required_checks_ready(results, "claude"))


if __name__ == "__main__":
    unittest.main()
