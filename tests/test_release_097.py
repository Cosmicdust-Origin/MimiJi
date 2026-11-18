"""MimiJi 0.9.7: reliable Claude resume after changing translation profiles."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.core import JobConfig
from local_subtitle_tool.preflight import LOGIN_REQUIRED, claude_status
from local_subtitle_tool.providers import (
    ClaudeProvider,
    ProviderFailureKind,
    classify_provider_failure,
)
from local_subtitle_tool.translator import (
    CLAUDE_REVIEW_REQUEST_TIMEOUT_SECONDS,
    REVIEW_REQUEST_TIMEOUT_SECONDS,
    _review_request_timeout,
)


class ClaudeResumeRegressionTests(unittest.TestCase):
    def test_claude_child_ignores_api_routing_overrides(self) -> None:
        inherited = {
            "PATH": "example-path",
            "ANTHROPIC_BASE_URL": "http://localhost:34910",
            "ANTHROPIC_API_KEY": "api-key-must-not-leak",
            "ANTHROPIC_AUTH_TOKEN": "auth-token-must-not-leak",
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-token-stays-available",
        }
        with patch.dict(os.environ, inherited, clear=True):
            environment = ClaudeProvider().subprocess_environment()

        self.assertEqual(environment["PATH"], "example-path")
        self.assertNotIn("ANTHROPIC_BASE_URL", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", environment)
        self.assertEqual(
            environment["CLAUDE_CODE_OAUTH_TOKEN"], "subscription-token-stays-available"
        )

    def test_claude_login_uses_the_same_clean_environment(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"PATH": "example-path", "ANTHROPIC_BASE_URL": "http://localhost:34910"},
                clear=True,
            ),
            patch(
                "local_subtitle_tool.providers.resolve_executable",
                return_value=Path("claude.exe"),
            ),
            patch(
                "local_subtitle_tool.providers.prepare_command",
                return_value=["claude.exe", "auth", "login"],
            ),
            patch("local_subtitle_tool.providers.subprocess.Popen") as popen,
        ):
            ClaudeProvider().launch_login()

        environment = popen.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_BASE_URL", environment)
        self.assertEqual(environment["PATH"], "example-path")

    def test_claude_connection_and_expired_oauth_are_classified(self) -> None:
        self.assertEqual(
            classify_provider_failure(
                "API Error: Unable to connect to API (ConnectionRefused)"
            ),
            ProviderFailureKind.TRANSIENT_ERROR,
        )
        self.assertEqual(
            classify_provider_failure(
                "Failed to authenticate. 401 OAuth access token has expired. "
                "Re-authenticate to continue."
            ),
            ProviderFailureKind.AUTH_REQUIRED,
        )

    def test_environment_diagnosis_rejects_expired_claude_oauth(self) -> None:
        status_payload = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "subscriptionType": "pro",
            }
        )
        result = subprocess.CompletedProcess(
            ["claude"], 0, stdout=status_payload, stderr=""
        )
        with (
            patch("local_subtitle_tool.preflight.resolve_executable", return_value=Path("claude")),
            patch("local_subtitle_tool.preflight.run_command", return_value=result),
            patch(
                "local_subtitle_tool.preflight._claude_oauth_expiry_ms", return_value=1
            ),
        ):
            status = claude_status()

        self.assertFalse(status.ok)
        self.assertEqual(status.status, LOGIN_REQUIRED)
        self.assertIn("만료", status.message)
        self.assertNotIn("accessToken", repr(status.details))

    def test_claude_review_waits_for_the_real_cli_result(self) -> None:
        claude = JobConfig(
            video_path=Path("video.mp4"),
            code="TEST-097",
            output_dir=Path("output"),
            provider="claude",
            model="sonnet",
        )
        codex = JobConfig(
            video_path=Path("video.mp4"),
            code="TEST-097",
            output_dir=Path("output"),
            provider="codex",
            model="gpt-5.6-luna",
        )

        self.assertEqual(
            _review_request_timeout(claude), CLAUDE_REVIEW_REQUEST_TIMEOUT_SECONDS
        )
        self.assertEqual(_review_request_timeout(codex), REVIEW_REQUEST_TIMEOUT_SECONDS)
        self.assertGreater(
            CLAUDE_REVIEW_REQUEST_TIMEOUT_SECONDS, REVIEW_REQUEST_TIMEOUT_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
