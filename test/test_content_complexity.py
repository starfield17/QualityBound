from __future__ import annotations

import unittest
from pathlib import Path

from core.smart.v1.sampling.complexity import (
    ComplexityProbeError,
    SCENE_CHANGE_THRESHOLD,
    build_scene_guard_command,
    build_scout_command,
    parse_scene_guard_metadata,
    parse_scout_metadata,
)


def _metadata() -> str:
    return """frame:0    pts:0       pts_time:0
lavfi.siti.si=1.0
lavfi.siti.ti=99.0
lavfi.scd.score=0.0
frame:1    pts:1       pts_time:0.083333
lavfi.siti.si=2.0
lavfi.siti.ti=4.0
lavfi.scd.score=2.0
frame:2    pts:2       pts_time:0.166667
lavfi.siti.si=10.0
lavfi.siti.ti=50.0
lavfi.scd.score=12.0
lavfi.scd.time=0.166667
frame:3    pts:3       pts_time:0.250000
lavfi.siti.si=3.0
lavfi.siti.ti=6.0
lavfi.scd.score=1.0
"""


class ContentComplexityCommandTest(unittest.TestCase):
    def test_scout_uses_low_resolution_siti_scdet_and_metadata_file(self) -> None:
        command = build_scout_command(
            Path("ffmpeg"), Path("source.mp4"), start_sec=3.0, duration_sec=2.0, metadata_path=Path("metrics.txt")
        )
        graph = command[command.index("-vf") + 1]
        self.assertIn("fps=12", graph)
        self.assertIn("min(480", graph)
        self.assertIn("siti", graph)
        self.assertIn(f"scdet=threshold={SCENE_CHANGE_THRESHOLD:g}", graph)
        self.assertIn("metadata=mode=print:file='metrics.txt'", graph)
        self.assertEqual(command.count("-map"), 1)
        self.assertEqual(command[command.index("-map") : command.index("-map") + 2], ["-map", "0:v:0"])
        self.assertEqual(command[-3:], ["-f", "null", "-"])

    def test_scene_guard_does_not_run_siti(self) -> None:
        command = build_scene_guard_command(
            Path("ffmpeg"), Path("source.mp4"), start_sec=3.0, duration_sec=2.0, metadata_path=Path("cuts.txt")
        )
        graph = command[command.index("-vf") + 1]
        self.assertNotIn(",siti,", graph)
        self.assertIn("scdet=threshold=10", graph)
        self.assertEqual(command.count("-map"), 1)
        self.assertEqual(command[command.index("-map") : command.index("-map") + 2], ["-map", "0:v:0"])


class ContentComplexityParsingTest(unittest.TestCase):
    def test_scout_excludes_first_and_cut_ti_and_uses_p90(self) -> None:
        metrics = parse_scout_metadata(_metadata())
        self.assertEqual(metrics.frame_count, 4)
        self.assertEqual(metrics.si_p90, 10.0)
        self.assertEqual(metrics.ti_p90, 6.0)
        self.assertEqual(metrics.scene_cut_times, (0.166667,))
        self.assertEqual(metrics.max_scene_score, 12.0)

    def test_malformed_missing_or_non_finite_metadata_fails_closed(self) -> None:
        with self.assertRaises(ComplexityProbeError):
            parse_scout_metadata("frame:0\nlavfi.siti.si=nan\nlavfi.siti.ti=1\nlavfi.scd.score=0\n")
        with self.assertRaises(ComplexityProbeError):
            parse_scout_metadata("frame:0\nlavfi.siti.si=1\nlavfi.siti.ti=1\n")
        with self.assertRaises(ComplexityProbeError):
            parse_scout_metadata("frame:0\nlavfi.siti.si=1\nlavfi.siti.ti=1\nlavfi.scd.score=10\n")

    def test_scene_only_parser_requires_score_and_cut_time(self) -> None:
        self.assertEqual(parse_scene_guard_metadata(_metadata()), (0.166667,))
        with self.assertRaises(ComplexityProbeError):
            parse_scene_guard_metadata("frame:0\nlavfi.scd.score=11\n")


if __name__ == "__main__":
    unittest.main()
