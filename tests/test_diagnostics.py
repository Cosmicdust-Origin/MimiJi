from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_subtitle_tool.diagnostics import (
    DIAGNOSTIC_SCHEMA_VERSION,
    DiagnosticReport,
    HardwareSnapshot,
    TranscriptionRecommendation,
    hardware_fingerprint,
    recommend_transcription,
    recommended_cpu_threads,
    run_quick_diagnostic,
)
from local_subtitle_tool.preflight import BLOCKED, CheckResult


def _hardware(*, driver: str = "555.10") -> HardwareSnapshot:
    return HardwareSnapshot(
        cpu_model="Test CPU",
        physical_cores=8,
        logical_cores=16,
        avx2=True,
        total_memory_bytes=32 * 1024**3,
        gpu_devices=(
            {
                "index": 0,
                "name": "Test GPU",
                "driver_version": driver,
                "memory_total_mib": 12288,
            },
        ),
        ctranslate2_version="4.8.2",
        python_version="3.14.0",
        app_version="0.1.0",
    )


class RecommendationTests(unittest.TestCase):
    def test_cuda_float16_has_priority(self) -> None:
        cuda = CheckResult(
            "cuda",
            True,
            "ok",
            {"supported_compute_types": ["float16", "int8"]},
        )
        cpu = CheckResult("cpu_runtime", True, "ok", {"supported_compute_types": ["int8"]})
        result = recommend_transcription(cuda, cpu, logical_cores=16)
        self.assertEqual(result.accelerator, "cuda")
        self.assertEqual(result.compute_type, "float16")
        self.assertFalse(result.requires_confirmation)

    def test_cpu_int8_is_explicit_fallback(self) -> None:
        cuda = CheckResult("cuda", False, "missing", status=BLOCKED)
        cpu = CheckResult("cpu_runtime", True, "ok", {"supported_compute_types": ["int8"]})
        result = recommend_transcription(cuda, cpu, logical_cores=16)
        self.assertEqual(result.accelerator, "cpu")
        self.assertEqual(result.compute_type, "int8")
        self.assertEqual(result.cpu_threads, 15)
        self.assertTrue(result.requires_confirmation)

    def test_cpu_thread_default_keeps_one_thread_free(self) -> None:
        self.assertEqual(recommended_cpu_threads(16), 15)
        self.assertEqual(recommended_cpu_threads(1), 1)


class DiagnosticPersistenceTests(unittest.TestCase):
    def test_driver_change_changes_hardware_fingerprint(self) -> None:
        self.assertNotEqual(
            hardware_fingerprint(_hardware(driver="555.10")),
            hardware_fingerprint(_hardware(driver="556.20")),
        )

    @patch("local_subtitle_tool.diagnostics.resolve_executable", return_value=Path("ffprobe"))
    @patch("local_subtitle_tool.diagnostics.check_cpu_runtime")
    @patch("local_subtitle_tool.diagnostics.check_cuda")
    @patch("local_subtitle_tool.diagnostics.collect_hardware_snapshot")
    def test_quick_diagnostic_requests_full_scan_after_hardware_change(
        self,
        collect_hardware,
        check_cuda,
        check_cpu,
        _resolve,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            original = _hardware(driver="555.10")
            changed = _hardware(driver="556.20")
            report = DiagnosticReport(
                schema_version=DIAGNOSTIC_SCHEMA_VERSION,
                created_at="2026-11-14T00:00:00+00:00",
                hardware=original,
                hardware_fingerprint=hardware_fingerprint(original),
                checks=[],
                recommendation=TranscriptionRecommendation(
                    True, "cuda", 0, "float16", None, False, "ok"
                ),
                selected_provider="codex",
            )
            from local_subtitle_tool.core import atomic_write_json
            from local_subtitle_tool.diagnostics import diagnostic_report_path

            atomic_write_json(diagnostic_report_path(state_dir), report.to_dict())
            collect_hardware.return_value = changed
            check_cuda.return_value = CheckResult(
                "cuda", True, "ok", {"supported_compute_types": ["float16"]}
            )
            check_cpu.return_value = CheckResult(
                "cpu_runtime", True, "ok", {"supported_compute_types": ["int8"]}
            )
            result = run_quick_diagnostic(state_dir=state_dir)
            self.assertTrue(result.full_diagnostic_required)
            self.assertIn("변경", result.reasons[0])


if __name__ == "__main__":
    unittest.main()
