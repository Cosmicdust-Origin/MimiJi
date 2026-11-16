from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from local_subtitle_tool.preflight import CheckResult
from local_subtitle_tool.providers import (
    ClaudeProvider,
    CodexProvider,
    ProviderCancelled,
    ProviderError,
    ProviderFailureKind,
    TranslationProvider,
    classify_provider_failure,
)


class SleepingProvider(TranslationProvider):
    name = "sleeping"

    def status(self) -> CheckResult:
        return CheckResult("sleeping", True, "ready")

    def login_command(self) -> list[str]:
        return []

    def build_command(
        self,
        *,
        model: str,
        reasoning_effort: str,
        speed_mode: str,
        schema_path: Path,
        output_path: Path,
        workdir: Path,
    ) -> list[str]:
        del model, reasoning_effort, speed_mode, schema_path, output_path, workdir
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    def parse_output(self, *, stdout: str, output_path: Path) -> dict[str, object]:
        del stdout, output_path
        return {}


class UsageLimitProvider(SleepingProvider):
    name = "usage-limit"

    def build_command(
        self,
        *,
        model: str,
        reasoning_effort: str,
        speed_mode: str,
        schema_path: Path,
        output_path: Path,
        workdir: Path,
    ) -> list[str]:
        del model, reasoning_effort, speed_mode, schema_path, output_path, workdir
        script = (
            "import sys; prompt=sys.stdin.read(); "
            "sys.stderr.write('OpenAI Codex session\\nuser\\n' + prompt + "
            "'\\nERROR: usage limit reached; retry later\\n'); sys.exit(1)"
        )
        return [sys.executable, "-c", script]


class ProviderTests(unittest.TestCase):
    def test_codex_command_has_safety_and_exact_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            output = root / "output.json"
            command = CodexProvider().build_command(
                model="gpt-5.6-luna",
                reasoning_effort="high",
                speed_mode="fast",
                schema_path=schema,
                output_path=output,
                workdir=root,
            )
            self.assertIn("read-only", command)
            self.assertIn("gpt-5.6-luna", command)
            self.assertIn("--output-schema", command)
            self.assertIn('model_reasoning_effort="high"', command)
            self.assertIn('service_tier="fast"', command)
            self.assertIn("fast_mode", command)
            self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_claude_command_includes_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            command = ClaudeProvider().build_command(
                model="sonnet",
                reasoning_effort="xhigh",
                speed_mode="standard",
                schema_path=schema,
                output_path=root / "unused.json",
                workdir=root,
            )
            self.assertIn("--effort", command)
            self.assertIn("xhigh", command)
            self.assertIn("--no-session-persistence", command)

    def test_failure_classification_is_conservative(self) -> None:
        self.assertEqual(
            classify_provider_failure("I cannot comply with this request", invalid_output=True),
            ProviderFailureKind.CONTENT_REFUSAL,
        )
        self.assertEqual(
            classify_provider_failure("malformed json", invalid_output=True),
            ProviderFailureKind.INVALID_OUTPUT,
        )
        self.assertEqual(
            classify_provider_failure("safety was mentioned", invalid_output=True),
            ProviderFailureKind.INVALID_OUTPUT,
        )

    def test_claude_structured_output(self) -> None:
        payload = {"items": [{"raw_id": 0}]}
        envelope = json.dumps({"structured_output": payload})
        result = ClaudeProvider().parse_output(stdout=envelope, output_path=Path("unused"))
        self.assertEqual(result, payload)

    def test_running_provider_process_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            cancel_event = threading.Event()
            cancel_event.set()
            started = time.monotonic()
            with self.assertRaises(ProviderCancelled):
                SleepingProvider().run(
                    prompt="cancel test",
                    model="test",
                    schema_path=schema,
                    output_path=root / "output.json",
                    workdir=root,
                    cancel_event=cancel_event,
                )
            self.assertLess(time.monotonic() - started, 5.0)

    def test_usage_limit_error_never_exposes_prompt_in_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text("{}", encoding="utf-8")
            prompt_marker = "PROMPT-MUST-STAY-IN-INTERNAL-EVENTS"
            prompt = prompt_marker + (" 번역 지시" * 2000)

            with self.assertRaises(ProviderError) as raised:
                UsageLimitProvider().run(
                    prompt=prompt,
                    model="test",
                    schema_path=schema,
                    output_path=root / "output.json",
                    workdir=root,
                )

            error = raised.exception
            self.assertEqual(error.kind, ProviderFailureKind.USAGE_LIMIT)
            self.assertLessEqual(len(str(error)), 300)
            self.assertNotIn(prompt_marker, str(error))
            self.assertIn(prompt_marker, error.stderr)
            self.assertIn("usage limit reached", str(error))


if __name__ == "__main__":
    unittest.main()
