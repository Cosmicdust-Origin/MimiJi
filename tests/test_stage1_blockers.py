"""Regression tests for the Phase 8 pre-packaging blockers (stage 1)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from huggingface_hub.errors import CacheNotFound
from PySide6.QtCore import QSettings
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication

from local_subtitle_tool import installers
from local_subtitle_tool.bridge import AppBridge
from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths, JobStatus
from local_subtitle_tool.diarization import inspect_diarization_status
from local_subtitle_tool.jobs import JobRegistry
from local_subtitle_tool.preflight import (
    CheckResult,
    UnsafeCommandArgument,
    check_cuda,
    missing_cuda_runtime_libraries,
    prepare_command,
)
from local_subtitle_tool.providers import (
    ClaudeProvider,
    ProviderError,
    ProviderFailureKind,
    ProviderResult,
    classify_provider_failure,
)
from local_subtitle_tool.qml_gui import create_engine
from local_subtitle_tool.translator import (
    TRANSLATION_SCHEMA,
    TranslationPartialError,
    TranslationReviewRequired,
    pending_review_items,
    save_manual_translation,
    translate,
)


def _item(raw_id: int, action: str = "translated", reason: str | None = None) -> dict:
    return {
        "raw_id": raw_id,
        "ja_fixed": f"日本語{raw_id}",
        "ko": f"한국어 {raw_id}" if action == "translated" else "",
        "action": action,
        "merge_with_previous": False,
        "reason": reason,
        "register_note": None,
    }


def _prepare_job(root: Path, count: int = 3, batch_size: int = 180) -> JobConfig:
    video = root / "TEST-001.mp4"
    video.touch()
    config = JobConfig(
        video_path=video, code="TEST-001", output_dir=root / "out", batch_size=batch_size
    )
    paths = JobPaths(config)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.raw.write_text(
        json.dumps(
            {
                "meta": {},
                "segments": [
                    {
                        "id": raw_id,
                        "start": float(raw_id),
                        "end": float(raw_id) + 0.8,
                        "text": f"発話{raw_id}",
                        "no_speech_prob": 0.0,
                    }
                    for raw_id in range(count)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config


class ScriptedProvider:
    """Returns items per request from a script keyed by the request's target ids."""

    def __init__(self, script) -> None:
        self.script = script
        self.calls: list[list[int]] = []

    def status(self) -> CheckResult:
        return CheckResult("fake", True, "ready")

    def run(self, **kwargs) -> ProviderResult:
        prompt = str(kwargs["prompt"])
        payload = json.loads(prompt.split("입력 데이터:\n", 1)[1])
        ids = list(payload["target_raw_ids"])
        self.calls.append(ids)
        retry = "previous_review" in payload
        result = self.script(ids, retry, len(self.calls))
        if isinstance(result, ProviderError):
            raise result
        return ProviderResult(payload={"items": result}, stdout="", stderr="", elapsed_seconds=0)


class StartupWithoutHuggingFaceCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QQuickStyle.setStyle("Basic")
        cls.app = QApplication.instance() or QApplication([])

    def test_diarization_status_and_gui_load_without_cache_directory(self) -> None:
        missing = CacheNotFound("Cache directory not found", Path("C:/missing"))
        with (
            patch("huggingface_hub.scan_cache_dir", side_effect=missing),
            tempfile.TemporaryDirectory() as directory,
        ):
            status = inspect_diarization_status()
            self.assertFalse(status.model_ready)
            root = Path(directory)
            bridge = AppBridge(
                settings=QSettings(str(root / "s.ini"), QSettings.Format.IniFormat),
                registry=JobRegistry(root / "registry"),
                auto_diagnose=False,
            )
            engine = create_engine(bridge)
            self.addCleanup(engine.deleteLater)
            self.assertEqual(len(engine.rootObjects()), 1)


class ReviewRecoveryTests(unittest.TestCase):
    def test_review_item_resolved_by_automatic_retry(self) -> None:
        def script(ids, retry, call):
            if not retry:
                return [_item(i, "review", "판단 보류") if i == 1 else _item(i) for i in ids]
            return [_item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory))
            provider = ScriptedProvider(script)
            with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
                decisions = translate(config)
            self.assertEqual([item["action"] for item in decisions], ["translated"] * 3)
            self.assertIn("review_small", decisions[1]["source"])
            self.assertEqual(provider.calls, [[0, 1, 2], [1]])
            self.assertEqual(JobManifest.load_or_create(config).status, JobStatus.TRANSLATED)

    def test_unresolved_review_blocks_then_manual_input_completes(self) -> None:
        def script(ids, retry, call):
            return [_item(i, "review", "음성 불명확") if i == 1 else _item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory))
            provider = ScriptedProvider(script)
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(TranslationReviewRequired) as raised,
            ):
                translate(config)
            self.assertEqual(raised.exception.review_raw_ids, [1])
            # main batch + small-group retry + single retry
            self.assertEqual(provider.calls, [[0, 1, 2], [1], [1]])
            self.assertEqual(JobManifest.load_or_create(config).status, JobStatus.PARTIAL_REVIEW)
            pending = pending_review_items(config)
            self.assertEqual([item["raw_id"] for item in pending], [1])
            self.assertFalse(pending[0]["resolved"])

            save_manual_translation(config, 1, "発話1です", "첫 번째 발화입니다")
            self.assertTrue(pending_review_items(config)[0]["resolved"])

            with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
                decisions = translate(config)
            # Every request was served from cache; no new provider calls.
            self.assertEqual(len(provider.calls), 3)
            self.assertEqual(decisions[1]["source"], "manual")
            self.assertEqual(decisions[1]["ko"], "첫 번째 발화입니다")
            manifest = JobManifest.load_or_create(config)
            self.assertEqual(manifest.status, JobStatus.TRANSLATED)
            self.assertEqual(manifest.stats["manual_translation_count"], 1)

    def test_manual_input_is_ignored_when_raw_text_changed(self) -> None:
        def script(ids, retry, call):
            return [_item(i, "review", "불명확") if i == 1 else _item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory))
            provider = ScriptedProvider(script)
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(TranslationReviewRequired),
            ):
                translate(config)
            save_manual_translation(config, 1, "発話1", "발화 1")
            paths = JobPaths(config)
            raw = json.loads(paths.raw.read_text(encoding="utf-8"))
            raw["segments"][1]["text"] = "別の発話"
            paths.raw.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(TranslationReviewRequired),
            ):
                translate(config)

    def test_manual_input_rejects_kana_and_review_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory))
            with self.assertRaises(ValueError):
                save_manual_translation(config, 0, "発話", "やめて")
            with self.assertRaises(ValueError):
                save_manual_translation(config, 0, "発話", "(검토) 그만")

    def test_held_batch_can_be_resolved_manually(self) -> None:
        def script(ids, retry, call):
            if ids == [1]:
                return ProviderError(
                    "번역할 수 없습니다", kind=ProviderFailureKind.CONTENT_REFUSAL
                )
            return [_item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory), batch_size=1)
            provider = ScriptedProvider(script)
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(TranslationPartialError),
            ):
                translate(config)
            pending = pending_review_items(config)
            self.assertEqual([(item["raw_id"], item["kind"]) for item in pending], [(1, "held")])
            self.assertEqual(pending[0]["held_kind"], "모델 거절")
            self.assertIn("모델이 번역 배치 2을 거절했습니다", pending[0]["reason"])
            self.assertIn("번역할 수 없습니다", pending[0]["reason"])
            save_manual_translation(config, 1, "発話1", "발화 1")
            with patch("local_subtitle_tool.translator.get_provider", return_value=provider):
                decisions = translate(config)
            self.assertEqual(decisions[1]["source"], "manual")

    def test_repeated_invalid_output_is_held_and_later_batches_continue(self) -> None:
        def script(ids, retry, call):
            if ids == [1]:
                return [_item(5)]  # wrong raw_id -> INVALID_OUTPUT
            return [_item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory), batch_size=1)
            provider = ScriptedProvider(script)
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(TranslationPartialError),
            ):
                translate(config)
            self.assertEqual(provider.calls, [[0], [1], [1], [2]])
            refused = json.loads(JobPaths(config).refused_batches.read_text(encoding="utf-8"))
            self.assertEqual(refused["batches"][0]["failure_kind"], "INVALID_OUTPUT")

    def test_consecutive_held_batches_stop_translation(self) -> None:
        def script(ids, retry, call):
            return ProviderError("I cannot comply", kind=ProviderFailureKind.CONTENT_REFUSAL)

        with tempfile.TemporaryDirectory() as directory:
            config = _prepare_job(Path(directory), count=5, batch_size=1)
            provider = ScriptedProvider(script)
            with (
                patch("local_subtitle_tool.translator.get_provider", return_value=provider),
                self.assertRaises(ProviderError),
            ):
                translate(config)
            self.assertEqual(len(provider.calls), 3)
            self.assertEqual(
                JobManifest.load_or_create(config).status, JobStatus.TRANSLATION_BLOCKED
            )

    def test_invalid_action_is_rejected(self) -> None:
        from local_subtitle_tool.translator import _validate_items

        with self.assertRaises(ValueError):
            _validate_items({"items": [_item(0, "translate")]}, [0])


class BridgeReviewScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        QQuickStyle.setStyle("Basic")
        cls.app = QApplication.instance() or QApplication([])

    def test_review_error_opens_review_screen_with_items(self) -> None:
        def script(ids, retry, call):
            return [_item(i, "review", "불명확") if i == 2 else _item(i) for i in ids]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _prepare_job(root)
            with (
                patch(
                    "local_subtitle_tool.translator.get_provider",
                    return_value=ScriptedProvider(script),
                ),
                self.assertRaises(TranslationReviewRequired) as raised,
            ):
                translate(config)
            bridge = AppBridge(
                settings=QSettings(str(root / "s.ini"), QSettings.Format.IniFormat),
                registry=JobRegistry(root / "registry"),
                auto_diagnose=False,
            )
            bridge._active_config = config
            bridge._busy = True
            bridge._finish_operation("job", None, raised.exception)
            self.assertEqual(bridge.screen, "review")
            self.assertFalse(bridge.busy)
            self.assertEqual([item["raw_id"] for item in bridge.reviewItems], [2])
            self.assertEqual(bridge.reviewUnresolvedCount, 1)
            bridge.saveManualTranslation(2, "発話2", "발화 2")
            self.assertEqual(bridge.reviewUnresolvedCount, 0)

    def test_reasoning_options_follow_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bridge = AppBridge(
                settings=QSettings(str(root / "s.ini"), QSettings.Format.IniFormat),
                registry=JobRegistry(root / "registry"),
                auto_diagnose=False,
            )
            bridge.provider = "codex"
            bridge.reasoning = "max"
            self.assertNotEqual(bridge.reasoning, "max")
            bridge.reasoning = "xhigh"
            self.assertEqual(bridge.reasoning, "xhigh")
            bridge.provider = "claude"
            self.assertIn(bridge.reasoning, bridge.reasoningOptions)
            self.assertNotIn("xhigh", bridge.reasoningOptions)


class FrozenInstallerTests(unittest.TestCase):
    def test_frozen_build_never_runs_pip_through_the_app_executable(self) -> None:
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(installers, "launch_visible") as launch,
        ):
            with self.assertRaises(installers.PackagedInstallUnavailable):
                installers.install_python_dependencies(Path("."))
            with self.assertRaises(installers.PackagedInstallUnavailable):
                installers.install_diarization_dependencies(Path("."))
            launch.assert_not_called()


class CudaRuntimeDiagnosisTests(unittest.TestCase):
    def _fake_ctranslate2(self) -> types.ModuleType:
        module = types.ModuleType("ctranslate2")
        module.__version__ = "4.8.2"  # type: ignore[attr-defined]
        module.get_cuda_device_count = lambda: 1  # type: ignore[attr-defined]
        module.get_supported_compute_types = (  # type: ignore[attr-defined]
            lambda device, index=0: {"float16", "int8"}
        )
        return module

    def test_missing_cublas_blocks_cuda_even_when_device_is_detected(self) -> None:
        with (
            patch.dict(sys.modules, {"ctranslate2": self._fake_ctranslate2()}),
            patch("local_subtitle_tool.preflight.detect_gpu_details", return_value={}),
            patch(
                "local_subtitle_tool.preflight.missing_cuda_runtime_libraries",
                return_value=["cublas64_12.dll"],
            ),
        ):
            result = check_cuda()
        self.assertFalse(result.ok)
        self.assertIn("cublas64_12.dll", result.message)

    def test_loader_failure_is_reported_per_library(self) -> None:
        def loader(name: str) -> None:
            if name.startswith("cublasLt"):
                raise OSError("not found")

        missing = missing_cuda_runtime_libraries(loader)
        if os.name == "nt":
            self.assertEqual(missing, ["cublasLt64_12.dll"])
        else:
            self.assertEqual(missing, [])


@unittest.skipUnless(os.name == "nt", "cmd.exe shim behavior is Windows-only")
class CmdShimArgumentTests(unittest.TestCase):
    def _shim(self, root: Path) -> Path:
        script = root / "argv.py"
        script.write_text(
            "import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
            encoding="utf-8",
        )
        shim = root / "fakecli.cmd"
        shim.write_text(
            f'@echo off\r\n"{sys.executable}" "%~dp0argv.py" %*\r\n', encoding="utf-8"
        )
        return shim

    def test_compact_schema_and_empty_argument_survive_cmd_shim(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shim = self._shim(root)
            schema = json.dumps(TRANSLATION_SCHEMA, ensure_ascii=False, separators=(",", ":"))
            arguments = ["-p", "--json-schema", schema, "--model", "sonnet", "--tools", ""]
            result = subprocess.run(
                prepare_command(shim, *arguments),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            received = json.loads(result.stdout)
            self.assertEqual(received[:2], ["-p", "--json-schema"])
            self.assertEqual(json.loads(received[2]), TRANSLATION_SCHEMA)
            self.assertEqual(received[3:5], ["--model", "sonnet"])

    def test_multiline_argument_is_refused_for_cmd_shim(self) -> None:
        with self.assertRaises(UnsafeCommandArgument):
            prepare_command(Path(r"C:\tools\claude.cmd"), "--json-schema", "{\n}")

    def test_claude_command_passes_single_line_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            schema_path = Path(directory) / "schema.json"
            schema_path.write_text(json.dumps(TRANSLATION_SCHEMA, indent=2), encoding="utf-8")
            with patch(
                "local_subtitle_tool.providers.resolve_executable",
                return_value=Path(r"C:\npm\claude.cmd"),
            ):
                command = ClaudeProvider().build_command(
                    model="sonnet",
                    reasoning_effort="default",
                    speed_mode="standard",
                    schema_path=schema_path,
                    output_path=Path(directory) / "out.json",
                    workdir=Path(directory),
                )
            schema_argument = command[command.index("--json-schema") + 1]
            self.assertNotIn("\n", schema_argument)


class ProviderClassificationTests(unittest.TestCase):
    def test_korean_and_japanese_refusals_are_isolated(self) -> None:
        for text in ("이 내용은 번역할 수 없습니다.", "この内容は翻訳できません。"):
            self.assertEqual(
                classify_provider_failure(text, invalid_output=True),
                ProviderFailureKind.CONTENT_REFUSAL,
            )

    def test_claude_error_envelope_is_not_treated_as_success(self) -> None:
        envelope = json.dumps({"is_error": True, "result": "I can't help with that."})
        with self.assertRaises(ValueError):
            ClaudeProvider().parse_output(stdout=envelope, output_path=Path("unused"))


class AtomicReplaceRetryTests(unittest.TestCase):
    def test_transient_permission_error_is_retried(self) -> None:
        from local_subtitle_tool import core

        real_replace = os.replace
        failures = {"count": 0}

        def flaky_replace(source, target):
            if failures["count"] < 2:
                failures["count"] += 1
                raise PermissionError(32, "in use")
            return real_replace(source, target)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(core.os, "replace", side_effect=flaky_replace),
            patch.object(core.time, "sleep"),
        ):
            target = Path(directory) / "manifest.json"
            core.atomic_write_json(target, {"ok": True})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})
        self.assertEqual(failures["count"], 2)


class ManifestMigrationTests(unittest.TestCase):
    def test_schema2_review_required_becomes_partial_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "TEST-009.mp4"
            video.touch()
            config = JobConfig(video_path=video, code="TEST-009", output_dir=root / "out")
            paths = JobPaths(config)
            paths.state_dir.mkdir(parents=True)
            paths.manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "config": config.to_dict(),
                        "status": "REVIEW_REQUIRED",
                        "completed_batches": [0],
                        "completed_clips": [0, 1],
                    }
                ),
                encoding="utf-8",
            )
            manifest = JobManifest.load_or_create(config)
            self.assertEqual(manifest.status, JobStatus.PARTIAL_REVIEW)
            self.assertEqual(manifest.completed_clips, [0, 1])


if __name__ == "__main__":
    unittest.main()
