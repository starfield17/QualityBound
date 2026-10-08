from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.smart.v1.sampling.planner import PlannedWindow, SamplePlan
from core.smart.v1.sampling.scout import _align_plan


class SmartSamplingAlignmentTest(unittest.TestCase):
    def test_unscouted_validation_is_not_probed_or_shifted(self) -> None:
        blind = PlannedWindow("holdout:unscouted", 30.0, 5.0, ("unscouted_validation",))
        plan = SamplePlan((), (), (blind,), False)
        with tempfile.TemporaryDirectory() as directory:
            with patch("core.smart.v1.sampling.scout.build_scene_guard_command") as command:
                aligned = _align_plan(plan, ffmpeg_path=Path("ffmpeg"), source_path=Path("source"),
                                     source_duration_sec=60.0, temp_root=Path(directory),
                                     run_command=lambda *_args: None, progress=lambda *_args: None)
        command.assert_not_called()
        self.assertEqual(aligned.holdout_windows, (blind,))

    def test_scene_alignment_preserves_original_non_overlap_contract(self) -> None:
        plan = SamplePlan(
            scout_windows=(),
            search_windows=(
                PlannedWindow("search:a", 0.0, 5.0, ("highest_si",)),
                PlannedWindow("search:b", 5.0, 5.0, ("highest_ti",)),
            ),
            holdout_windows=(),
            whole_video=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch("core.smart.v1.sampling.scout._read_metadata", return_value="metadata"),
                patch(
                    "core.smart.v1.sampling.scout.parse_scene_guard_metadata",
                    side_effect=[(1.0,), ()],
                ),
            ):
                aligned = _align_plan(
                    plan,
                    ffmpeg_path=Path("ffmpeg"),
                    source_path=Path("source.mp4"),
                    source_duration_sec=20.0,
                    temp_root=Path(temp_dir),
                    run_command=lambda _command, _phase: None,
                    progress=lambda _state, _values: None,
                )

        first, second = aligned.search_windows
        self.assertEqual((first.start_sec, second.start_sec), (0.0, 5.0))
        self.assertTrue(first.crosses_scene_cut)
        self.assertLessEqual(first.start_sec + first.duration_sec, second.start_sec)


if __name__ == "__main__":
    unittest.main()
