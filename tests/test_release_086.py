"""MimiJi 0.8.6: even batch split across lanes, stable plan for resumed jobs."""

from __future__ import annotations

import json
import unittest

from test_parallel_lanes import LaneProvider, ParallelLaneTests, compact

from local_subtitle_tool.core import JobManifest, JobPaths
from local_subtitle_tool.translator import _even_batch_ranges


class LaneScalingTests(ParallelLaneTests):
    lane_limit = 6

    def test_lanes_grow_so_all_batches_go_out_in_one_round(self) -> None:
        from local_subtitle_tool.translator import _lane_count

        self.assertEqual(_lane_count(741, 3, 240), 4)  # XYZ-627: one round, not two
        self.assertEqual(_lane_count(303, 3, 240), 3)
        self.assertEqual(_lane_count(5000, 3, 240), 6)
        config = self.job(12, batch_size=2)
        provider = LaneProvider(lambda ids, n: compact(ids))
        self.run_with(provider, config, parallel_batches=3)
        self.assertEqual(len(provider.calls), 6)
        self.assertEqual({len(call["targets"]) for call in provider.calls}, {2})
        self.assertTrue(all(call["resume"] is None for call in provider.calls))  # one round


# Only the scaling test runs here; the inherited lane tests run in their own module.
for _name in dir(ParallelLaneTests):
    if _name.startswith("test_") and _name not in vars(LaneScalingTests):
        setattr(LaneScalingTests, _name, None)


class EvenSplitTests(unittest.TestCase):
    def test_batches_are_equal_and_fill_every_lane(self) -> None:
        self.assertEqual([b - a for a, b in _even_batch_ranges(303, 240, 3)], [101] * 3)
        sizes = [b - a for a, b in _even_batch_ranges(400, 240, 3)]
        self.assertEqual(sizes, [134, 133, 133])  # one round, every lane busy
        long_work = _even_batch_ranges(1000, 240, 3)
        self.assertEqual(len(long_work), 6)
        self.assertTrue(all(b - a <= 240 for a, b in long_work))


class PlanTests(ParallelLaneTests):
    def test_new_job_uses_even_plan_and_keeps_it(self) -> None:
        config = self.job(10, batch_size=8)
        provider = LaneProvider(lambda ids, n: compact(ids))
        self.run_with(provider, config, parallel_batches=2)
        self.assertEqual(sorted(len(call["targets"]) for call in provider.calls), [5, 5])
        plan = JobManifest.load_or_create(config).stats["translation_batch_plan"]["ranges"]
        self.assertEqual(plan, [[0, 5], [5, 10]])
        again = LaneProvider(lambda ids, n: compact(ids))
        self.run_with(again, config, parallel_batches=3)  # lane count changed: same plan
        self.assertEqual(again.calls, [])

    def test_job_started_before_086_keeps_fixed_size_batches(self) -> None:
        config = self.job(10, batch_size=8)
        paths = JobPaths(config)
        paths.batches_dir.mkdir(parents=True, exist_ok=True)
        (paths.batches_dir / "batch_0000.json").write_text(json.dumps({}), encoding="utf-8")
        provider = LaneProvider(lambda ids, n: compact(ids))
        self.run_with(provider, config, parallel_batches=2)
        self.assertEqual(sorted(len(call["targets"]) for call in provider.calls), [2, 8])


class JunkLineTests(ParallelLaneTests):
    def _job_with(self, texts: dict[int, tuple[str, dict]]):
        config = self.job(6, batch_size=6)
        paths = JobPaths(config)
        data = json.loads(paths.raw.read_text(encoding="utf-8"))
        for segment in data["segments"]:
            if segment["id"] in texts:
                text, extra = texts[segment["id"]]
                segment["text"] = text
                segment.update(extra)
        paths.raw.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return config

    def test_extreme_repetition_in_review_is_omitted_without_asking(self) -> None:
        config = self._job_with({2: ("怪我の" + "う" * 100, {"compression_ratio": 24.8})})
        provider = LaneProvider(lambda ids, n: compact(ids, review={2} if n == 0 else set()))
        decisions = self.run_with(provider, config, parallel_batches=1)
        self.assertEqual(len(provider.calls), 1)  # no review re-request for the loop
        self.assertEqual(decisions[2]["action"], "omitted")
        self.assertIn("반복 환각", decisions[2]["reason"])

    def test_laugh_symbol_may_be_omitted_as_laugh(self) -> None:
        config = self._job_with({2: ("笑", {"no_speech_prob": 0.9})})

        def script(ids, n):
            payload = compact([i for i in ids if i != 2])
            payload["omitted"] = [{"raw_id": 2, "code": "laugh"}]
            return payload

        provider = LaneProvider(script)
        decisions = self.run_with(provider, config, parallel_batches=1)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(decisions[2]["action"], "omitted")

    def test_timed_out_review_request_is_not_repeated(self) -> None:
        from local_subtitle_tool.providers import ProviderError, ProviderFailureKind
        from local_subtitle_tool.translator import TranslationReviewRequired

        config = self.job(6, batch_size=6)

        def script(ids, n):
            if n == 0:
                return compact(ids, review={2})
            raise ProviderError("timeout", kind=ProviderFailureKind.TRANSIENT_ERROR)

        provider = LaneProvider(script)
        with self.assertRaises(TranslationReviewRequired) as raised:
            self.run_with(provider, config, parallel_batches=1)
        self.assertEqual(raised.exception.review_raw_ids, [2])
        self.assertEqual(len(provider.calls), 2)  # one try, no second round


if __name__ == "__main__":
    unittest.main()


class SharedOutputFolderTests(unittest.TestCase):
    def test_two_videos_can_share_one_output_folder(self) -> None:
        import tempfile
        from pathlib import Path

        from local_subtitle_tool.core import JobConfig

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = root / "out"
            first_video, second_video = root / "AAA-001.mp4", root / "BBB-002.mp4"
            first_video.touch()
            second_video.touch()
            # A job from 0.8.6 or earlier kept its state directly in out/.subtitle_job.
            legacy = JobConfig(video_path=first_video, code="AAA-001", output_dir=out)
            legacy_state = out / ".subtitle_job"
            legacy_state.mkdir(parents=True)
            (legacy_state / "manifest.json").write_text(
                json.dumps({"schema_version": 5, "config": legacy.to_dict()}), encoding="utf-8"
            )
            self.assertEqual(JobPaths(legacy).state_dir, legacy_state)
            JobManifest.load_or_create(legacy)
            second = JobConfig(video_path=second_video, code="BBB-002", output_dir=out)
            manifest = JobManifest.load_or_create(second)  # used to raise "일치하지 않습니다"
            state = JobPaths(second).state_dir
            self.assertEqual(state.parent, legacy_state / "jobs")
            self.assertTrue(state.name.startswith("BBB-002-"))
            self.assertTrue((state / "manifest.json").is_file())
            self.assertEqual(manifest.config.code, "BBB-002")
            third = JobConfig(video_path=root / "CCC-003.mp4", code="CCC-003", output_dir=out)
            self.assertNotEqual(JobPaths(third).state_dir, state)
