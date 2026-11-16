from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from local_subtitle_tool.core import JobConfig, JobManifest, JobPaths, JobStatus
from local_subtitle_tool.preflight import BLOCKED, CheckResult
from local_subtitle_tool.transcriber import (
    SAMPLING_RATE,
    CPUFallbackConfirmationRequired,
    TranscriptionCancelled,
    _clip_windows,
    _resolve_runtime,
    _speech_groups,
    _window_speech_chunks,
    _window_speech_coverage,
    transcribe,
)


def _segment(start: float, end: float, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        seek=int(start * 100),
        start=start,
        end=end,
        text=text,
        tokens=[1, 2],
        avg_logprob=-0.1,
        compression_ratio=1.0,
        no_speech_prob=0.01,
        words=[
            SimpleNamespace(
                start=start,
                end=end,
                word=text,
                probability=0.99,
            )
        ],
    )


def _info() -> SimpleNamespace:
    return SimpleNamespace(language="ja", language_probability=0.99)


class ClipWindowTests(unittest.TestCase):
    def test_duration_is_split_into_stable_thirty_second_windows(self) -> None:
        windows = _clip_windows(65.0)
        self.assertEqual(
            [(item.index, item.start, item.end) for item in windows],
            [(0, 0.0, 30.0), (1, 30.0, 60.0), (2, 60.0, 65.0)],
        )

    def test_speech_crossing_a_boundary_stays_whole_in_its_start_window(self) -> None:
        windows = _clip_windows(65.0)
        chunks = [{"start": 29 * SAMPLING_RATE, "end": 31 * SAMPLING_RATE}]
        self.assertEqual(
            _window_speech_chunks(chunks, windows[0]),
            [{"start": 29 * SAMPLING_RATE, "end": 31 * SAMPLING_RATE}],
        )
        self.assertEqual(_window_speech_chunks(chunks, windows[1]), [])
        self.assertEqual(_window_speech_chunks(chunks, windows[2]), [])
        self.assertEqual(
            _window_speech_coverage(chunks, windows[1]),
            [{"start": 30.0, "end": 31.0}],
        )

    def test_close_speech_chunks_are_decoded_as_one_contiguous_group(self) -> None:
        chunks = [
            {"start": 1 * SAMPLING_RATE, "end": 3 * SAMPLING_RATE},
            {"start": 4 * SAMPLING_RATE, "end": 5 * SAMPLING_RATE},
            {"start": 9 * SAMPLING_RATE, "end": 10 * SAMPLING_RATE},
        ]
        self.assertEqual(
            _speech_groups(chunks),
            [
                {"start": 1 * SAMPLING_RATE, "end": 5 * SAMPLING_RATE},
                {"start": 9 * SAMPLING_RATE, "end": 10 * SAMPLING_RATE},
            ],
        )


class RuntimePolicyTests(unittest.TestCase):
    @patch("local_subtitle_tool.transcriber.check_cpu_runtime")
    @patch("local_subtitle_tool.transcriber.check_cuda")
    def test_auto_mode_never_silently_falls_back_to_cpu(self, check_cuda, check_cpu) -> None:
        check_cuda.return_value = CheckResult("cuda", False, "없음", status=BLOCKED)
        check_cpu.return_value = CheckResult(
            "cpu_runtime",
            True,
            "사용 가능",
            {"supported_compute_types": ["int8"]},
        )
        config = JobConfig(
            video_path=Path("TEST-001.mp4"),
            code="TEST-001",
            output_dir=Path("out"),
        )
        with self.assertRaises(CPUFallbackConfirmationRequired):
            _resolve_runtime(config)

    @patch("local_subtitle_tool.transcriber.check_cpu_runtime")
    def test_explicit_cpu_mode_uses_int8(self, check_cpu) -> None:
        check_cpu.return_value = CheckResult(
            "cpu_runtime",
            True,
            "사용 가능",
            {"supported_compute_types": ["int8"]},
        )
        config = JobConfig(
            video_path=Path("TEST-002.mp4"),
            code="TEST-002",
            output_dir=Path("out"),
            accelerator="cpu",
            cpu_threads=4,
        )
        runtime = _resolve_runtime(config)
        self.assertEqual(runtime.device, "cpu")
        self.assertEqual(runtime.compute_type, "int8")
        self.assertEqual(runtime.cpu_threads, 4)


def _speech(*ranges: tuple[float, float]) -> list[dict[str, int]]:
    return [
        {"start": int(start * SAMPLING_RATE), "end": int(end * SAMPLING_RATE)}
        for start, end in ranges
    ]


class CheckpointResumeTests(unittest.TestCase):
    def _config(self, root: Path, code: str) -> JobConfig:
        video = root / f"{code}.mp4"
        video.write_bytes(b"video")
        return JobConfig(
            video_path=video,
            code=code,
            output_dir=root / "out",
            accelerator="cpu",
            cpu_threads=4,
        )

    def _environment(self, root: Path, duration: float = 65.0):
        model_dir = root / "model"
        model_dir.mkdir(exist_ok=True)
        media = CheckResult(
            "media",
            True,
            "ok",
            {"duration": duration, "audio_streams": [{"codec_type": "audio"}]},
        )
        cpu = CheckResult("cpu_runtime", True, "ok", {"supported_compute_types": ["int8"]})
        audio = np.zeros(int(duration * SAMPLING_RATE), dtype=np.float32)
        return (
            patch("local_subtitle_tool.transcriber.probe_media", return_value=media),
            patch("local_subtitle_tool.transcriber.check_cpu_runtime", return_value=cpu),
            patch(
                "local_subtitle_tool.transcriber.inspect_model_cache",
                return_value=SimpleNamespace(ready=True, snapshot_dir=model_dir),
            ),
            patch("local_subtitle_tool.transcriber._decode_audio", return_value=audio),
        )

    def test_vad_is_applied_and_missing_clip_only_is_retranscribed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, "TEST-003")
            speech = _speech((1.0, 2.0), (31.0, 32.0), (61.0, 62.0))
            probe, cpu, model_cache, decode = self._environment(root)
            with (
                probe,
                cpu,
                model_cache,
                decode,
                patch("local_subtitle_tool.transcriber._detect_speech", return_value=speech),
                patch("faster_whisper.WhisperModel") as whisper_model,
            ):
                whisper_model.return_value.transcribe.side_effect = [
                    (iter([_segment(0.0, 1.0, "첫 번째")]), _info()),
                    (iter([_segment(0.0, 1.0, "두 번째")]), _info()),
                    (iter([_segment(0.0, 1.0, "세 번째")]), _info()),
                    (iter([_segment(0.0, 1.0, "세 번째 재개")]), _info()),
                ]
                transcribe(config)
                paths = JobPaths(config)
                self.assertEqual(len(list(paths.clips_dir.glob("*.json"))), 3)
                first_raw = json.loads(paths.raw.read_text(encoding="utf-8"))
                self.assertEqual([item["id"] for item in first_raw["segments"]], [0, 1, 2])
                self.assertEqual(
                    [(item["start"], item["end"]) for item in first_raw["segments"]],
                    [(1.0, 2.0), (31.0, 32.0), (61.0, 62.0)],
                )
                self.assertTrue(first_raw["meta"]["vad_applied"])
                first_call = whisper_model.return_value.transcribe.call_args_list[0]
                # Only the one second of detected speech is decoded, VAD is not
                # delegated to faster-whisper, and no clip_timestamps are used.
                self.assertEqual(len(first_call.args[0]), SAMPLING_RATE)
                self.assertFalse(first_call.kwargs["vad_filter"])
                self.assertNotIn("clip_timestamps", first_call.kwargs)

                paths.raw.unlink()
                (paths.clips_dir / "000002.json").unlink()
                transcribe(config)

                second_raw = json.loads(paths.raw.read_text(encoding="utf-8"))
                self.assertEqual([item["id"] for item in second_raw["segments"]], [0, 1, 2])
                self.assertEqual(
                    [item["text"] for item in second_raw["segments"]],
                    ["첫 번째", "두 번째", "세 번째 재개"],
                )
                self.assertEqual(whisper_model.return_value.transcribe.call_count, 4)
                manifest = JobManifest.load_or_create(config)
                self.assertEqual(manifest.completed_clips, [0, 1, 2])
                self.assertEqual(manifest.status, JobStatus.TRANSCRIBED)

    def test_silent_window_is_checkpointed_without_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, "TEST-005")
            probe, cpu, model_cache, decode = self._environment(root, duration=60.0)
            with (
                probe,
                cpu,
                model_cache,
                decode,
                patch(
                    "local_subtitle_tool.transcriber._detect_speech",
                    return_value=_speech((40.0, 41.0)),
                ),
                patch("faster_whisper.WhisperModel") as whisper_model,
            ):
                whisper_model.return_value.transcribe.return_value = (
                    iter([_segment(0.0, 1.0, "はい")]),
                    _info(),
                )
                transcribe(config)
                self.assertEqual(whisper_model.return_value.transcribe.call_count, 1)
                clip0 = json.loads(
                    (JobPaths(config).clips_dir / "000000.json").read_text(encoding="utf-8")
                )
                self.assertEqual(clip0["segments"], [])

    def test_audible_gap_gets_no_vad_rescue_without_duplicating_existing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, "TEST-009")
            probe, cpu, model_cache, _decode = self._environment(root, duration=30.0)
            audio = np.zeros(30 * SAMPLING_RATE, dtype=np.float32)
            audio[9 * SAMPLING_RATE : 21 * SAMPLING_RATE] = 0.03
            with (
                probe,
                cpu,
                model_cache,
                patch("local_subtitle_tool.transcriber._decode_audio", return_value=audio),
                patch(
                    "local_subtitle_tool.transcriber._detect_speech",
                    return_value=_speech((1.0, 6.0), (24.0, 29.0)),
                ),
                patch("faster_whisper.WhisperModel") as whisper_model,
                patch("faster_whisper.BatchedInferencePipeline") as batched_pipeline,
            ):
                whisper_model.return_value.transcribe.side_effect = [
                    (iter([_segment(0.0, 1.0, "앞 대사")]), _info()),
                    (iter([_segment(0.0, 1.0, "뒤 대사")]), _info()),
                ]
                batched_pipeline.return_value.transcribe.return_value = (
                    iter([_segment(10.0, 12.0, "작은 대사")]),
                    _info(),
                )

                transcribe(config)

            rescue_call = batched_pipeline.return_value.transcribe.call_args
            self.assertEqual(
                rescue_call.kwargs["clip_timestamps"],
                [{"start": 0.0, "end": 30.0}],
            )
            raw = json.loads(JobPaths(config).raw.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["text"] for item in raw["segments"]],
                ["앞 대사", "작은 대사", "뒤 대사"],
            )
            rescued = [item for item in raw["segments"] if item.get("rescue")]
            self.assertEqual([item["text"] for item in rescued], ["작은 대사"])
            self.assertEqual(raw["meta"]["rescue_pass"]["windows"], 1)
            self.assertEqual(raw["meta"]["rescue_pass"]["intervals"], 1)

    def test_raw_is_not_reused_after_source_video_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, "TEST-006")
            probe, cpu, model_cache, decode = self._environment(root, duration=30.0)
            with (
                probe,
                cpu,
                model_cache,
                decode,
                patch(
                    "local_subtitle_tool.transcriber._detect_speech",
                    return_value=_speech((1.0, 2.0)),
                ),
                patch("faster_whisper.WhisperModel") as whisper_model,
            ):
                whisper_model.return_value.transcribe.side_effect = [
                    (iter([_segment(0.0, 1.0, "前")]), _info()),
                    (iter([_segment(0.0, 1.0, "後")]), _info()),
                ]
                transcribe(config)
                config.video_path.write_bytes(b"re-encoded video with another size")
                transcribe(config)
                raw = json.loads(JobPaths(config).raw.read_text(encoding="utf-8"))
                self.assertEqual([item["text"] for item in raw["segments"]], ["後"])

    def test_foreign_raw_in_output_folder_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, "TEST-007")
            paths = JobPaths(config)
            config.output_dir.mkdir(parents=True)
            paths.published_raw.write_text(
                json.dumps(
                    {"meta": {}, "segments": [{"id": 0, "text": "古い"}]}, ensure_ascii=False
                ),
                encoding="utf-8",
            )
            probe, cpu, model_cache, decode = self._environment(root, duration=30.0)
            with (
                probe,
                cpu,
                model_cache,
                decode,
                patch(
                    "local_subtitle_tool.transcriber._detect_speech",
                    return_value=_speech((1.0, 2.0)),
                ),
                patch("faster_whisper.WhisperModel") as whisper_model,
            ):
                whisper_model.return_value.transcribe.return_value = (
                    iter([_segment(0.0, 1.0, "新しい")]),
                    _info(),
                )
                transcribe(config)
            raw = json.loads(paths.raw.read_text(encoding="utf-8"))
            self.assertEqual([item["text"] for item in raw["segments"]], ["新しい"])
            # The foreign file in the output folder is left untouched.
            self.assertIn("古い", paths.published_raw.read_text(encoding="utf-8"))

    def test_cancel_preserves_only_finished_clip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, "TEST-004")
            probe, cpu, model_cache, decode = self._environment(root)
            cancel_event = threading.Event()

            def cancel_after_first_clip(fraction: float | None, _message: str) -> None:
                if fraction is not None and fraction >= 1 / 3:
                    cancel_event.set()

            with (
                probe,
                cpu,
                model_cache,
                decode,
                patch(
                    "local_subtitle_tool.transcriber._detect_speech",
                    return_value=_speech((1.0, 2.0), (31.0, 32.0), (61.0, 62.0)),
                ),
                patch("faster_whisper.WhisperModel") as whisper_model,
                self.assertRaises(TranscriptionCancelled),
            ):
                whisper_model.return_value.transcribe.side_effect = lambda *a, **k: (
                    iter([_segment(0.0, 1.0, "발화")]),
                    _info(),
                )
                transcribe(
                    config,
                    cancel_event=cancel_event,
                    progress_value=cancel_after_first_clip,
                )

            paths = JobPaths(config)
            self.assertTrue((paths.clips_dir / "000000.json").is_file())
            self.assertFalse((paths.clips_dir / "000001.json").exists())
            self.assertFalse(paths.raw.exists())
            manifest = JobManifest.load_or_create(config)
            self.assertEqual(manifest.completed_clips, [0])
            self.assertEqual(manifest.status, JobStatus.CANCELLED)
            self.assertEqual(manifest.stats.get("current_stage"), "transcription")
            self.assertGreater(manifest.active_elapsed_seconds, 0.0)

    def test_non_device_failure_is_not_reported_as_cuda_problem(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, "TEST-008")
            probe, cpu, model_cache, decode = self._environment(root, duration=30.0)
            with (
                probe,
                cpu,
                model_cache,
                decode,
                patch(
                    "local_subtitle_tool.transcriber._detect_speech",
                    return_value=_speech((1.0, 2.0)),
                ),
                patch("faster_whisper.WhisperModel") as whisper_model,
                patch(
                    "local_subtitle_tool.transcriber._save_checkpoint",
                    side_effect=PermissionError(13, "denied"),
                ),
                self.assertRaises(PermissionError),
            ):
                whisper_model.return_value.transcribe.return_value = (
                    iter([_segment(0.0, 1.0, "はい")]),
                    _info(),
                )
                transcribe(config)
            self.assertEqual(JobManifest.load_or_create(config).status, JobStatus.FAILED)

if __name__ == "__main__":
    unittest.main()
