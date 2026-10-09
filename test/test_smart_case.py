from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.models import (
    BackendChoice,
    CodecChoice,
    CompressionMode,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
    MediaInfo,
)
from core.smart.v1.evaluation import (
    calculate_case_metrics,
    is_false_size_block,
    is_quality_false_pass,
)
from core.smart.v1.vmaf import VMAF_STANDARD_MODEL, VmafEncodeMetadata
from scripts.run_smart_case import (
    OraclePoint,
    align_cfr_command,
    require_complete_encode,
    compute_full_vmaf_metrics,
    compute_segmented_vmaf_metrics,
    parse_smart_log,
    run_oracle_search,
    validate_constant_fps,
)


class TestSmartTimingParser(unittest.TestCase):
    """Tests for structured phase log [smart phase] JSON parsing and counter extraction."""

    def test_parse_smart_log_phases_and_counts(self) -> None:
        log_content = (
            "scout_windows=12\n"
            "search_windows=4\n"
            "holdout_windows=2\n"
            "reserve_windows=2\n"
            "$ ffmpeg -i in.mkv\n"
            '[smart phase] {"phase": "scout_probe", "seconds": 0.45}\n'
            '[smart phase] {"phase": "candidate encode", "seconds": 1.25}\n'
            '[smart phase] {"phase": "VMAF scoring", "seconds": 0.85}\n'
            '[smart phase] {"phase": "candidate encode", "seconds": 1.10}\n'
            '[smart phase] {"phase": "VMAF scoring", "seconds": 0.80}\n'
            "holdout=holdout-1 bitrate=1200000 VMAF=96.200 result=PASS\n"
            '[smart phase] {"phase": "VMAF scoring", "seconds": 0.90}\n'
            '[smart phase] {"phase": "representative size calibration", "seconds": 0.60}\n'
            "[smart timing] Smart total: 5.95s\n"
        )
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "smart.log"
            log_path.write_text(log_content, encoding="utf-8")
            parsed = parse_smart_log(log_path)

        counts = parsed["counts"]
        phase_seconds = parsed["phase_seconds"]

        self.assertEqual(counts["scout_windows"], 12)
        self.assertEqual(counts["quality_encodes"], 2)
        self.assertEqual(counts["vmaf_measurements"], 3)
        self.assertEqual(counts["holdout_measurements"], 1)
        self.assertEqual(counts["size_calibration_encodes"], 1)

        self.assertAlmostEqual(phase_seconds["scout_probe"], 0.45)
        self.assertAlmostEqual(phase_seconds["candidate encode"], 2.35)
        self.assertAlmostEqual(phase_seconds["VMAF scoring"], 2.55)
        self.assertAlmostEqual(phase_seconds["representative size calibration"], 0.60)

    def test_parse_cache_hits(self) -> None:
        log_content = (
            "[smart timing] reference cache hit: ref-a1.mkv\n"
            "[smart timing] reference cache hit: ref-a2.mkv\n"
            "[smart timing] measurement cache hit: hash-xyz-1\n"
            "[smart timing] measurement cache hit: hash-xyz-2\n"
            "[smart timing] measurement cache hit: hash-xyz-3\n"
        )
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "smart.log"
            log_path.write_text(log_content, encoding="utf-8")
            parsed = parse_smart_log(log_path)

        hits = parsed["cache_hits"]
        self.assertEqual(hits["reference_cache_hits"], 2)
        self.assertEqual(hits["measurement_cache_hits"], 3)
        self.assertEqual(hits["total_cache_hits"], 5)

    def test_two_pass_quality_encode_counting(self) -> None:
        log_content = (
            '[smart phase] {"phase": "candidate pass 1", "seconds": 0.50}\n'
            '[smart phase] {"phase": "candidate pass 2", "seconds": 0.70}\n'
            '[smart phase] {"phase": "VMAF scoring", "seconds": 0.40}\n'
            '[smart phase] {"phase": "candidate pass 1", "seconds": 0.52}\n'
            '[smart phase] {"phase": "candidate pass 2", "seconds": 0.72}\n'
            '[smart phase] {"phase": "VMAF scoring", "seconds": 0.41}\n'
        )
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "smart.log"
            log_path.write_text(log_content, encoding="utf-8")
            parsed = parse_smart_log(log_path)

        # In two-pass mode, candidate pass 2 produces the actual output
        self.assertEqual(parsed["counts"]["quality_encodes"], 2)
        self.assertEqual(parsed["counts"]["vmaf_measurements"], 2)

    def test_resilience_to_malformed_and_empty_log_lines(self) -> None:
        log_content = (
            "[smart phase] not-json\n"
            '[smart phase] {"phase": 123, "seconds": "invalid"}\n'
            "scout_windows=not-an-int\n"
            "\n"
            "random junk\n"
        )
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "smart.log"
            log_path.write_text(log_content, encoding="utf-8")
            parsed = parse_smart_log(log_path)

        self.assertEqual(parsed["counts"]["scout_windows"], 0)
        self.assertEqual(parsed["counts"]["quality_encodes"], 0)
        self.assertEqual(parsed["counts"]["vmaf_measurements"], 0)
        self.assertEqual(parsed["counts"]["holdout_measurements"], 0)
        self.assertEqual(parsed["counts"]["size_calibration_encodes"], 0)


class TestOracleBracketMonotonicLogic(unittest.TestCase):
    """Tests for oracle sweep bracket detection, refinement, and monotonic logic."""

    def _sample_item(self, temp_dir: Path) -> EncodePlanItem:
        src = temp_dir / "sample.mkv"
        src.write_bytes(b"\x00" * 1024)
        media = MediaInfo(
            path=src,
            duration=10.0,
            format_bitrate_bps=2_000_000,
            video_bitrate_bps=1_800_000,
            audio_bitrate_bps=128_000,
            width=1920,
            height=1080,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
        )
        enc_info = EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="fast",
        )
        options = EncodeOptions(
            codec=CodecChoice.HEVC,
            compression_mode=CompressionMode.SMART,
            min_vmaf=95.0,
            max_output_ratio=0.8,
        )
        return EncodePlanItem(
            source_path=src,
            output_path=temp_dir / "out.mp4",
            media_info=media,
            encoder_info=enc_info,
            options=options,
        )

    def test_strictly_increasing_bracket_and_refinement(self) -> None:
        """Verify bracket [lower, upper] identification and bounded bisection refinement."""
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            item = self._sample_item(temp_dir)
            target_vmaf = 95.0

            # Mock points for deterministic testing without external ffmpeg
            # Curve: VMAF = 85 + (bitrate_bps / 1_000_000) * 8.0
            def mock_vmaf(bps: int) -> float:
                return 85.0 + (bps / 1_000_000.0) * 8.0

            eval_cache: dict[int, OraclePoint] = {}

            with patch("scripts.run_smart_case.build_encode_commands", return_value=([], None)), \
                 patch("scripts.run_smart_case.subprocess.run", return_value=MagicMock(returncode=0)), \
                 patch("scripts.run_smart_case.compute_full_vmaf_metrics") as mock_metrics:

                def compute_side_effect(json_path: Path, *args: object, expected_frames: int | None = None) -> tuple[float, float, float]:
                    # Extract bps from json_path name
                    name = json_path.stem
                    bps = int(name.rsplit("_", 1)[-1])
                    score = mock_vmaf(bps)
                    return score, score, score

                mock_metrics.side_effect = compute_side_effect

                # Fake out video file existence
                with patch.object(Path, "is_file", return_value=True), \
                     patch.object(Path, "stat") as mock_stat:
                    mock_stat.return_value = MagicMock(st_size=500_000)

                    oracle_min, bracket = run_oracle_search(
                        ffmpeg_path=Path("/bin/ffmpeg"),
                        item=item,
                        workdir=temp_dir,
                        target_min_vmaf=target_vmaf,
                        fps=30.0,
                        model_spec=VMAF_STANDARD_MODEL,
                        encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
                        evaluated_cache=eval_cache,
                        executed_commands=[],
                        max_coarse_points=6,
                        max_refinements=3,
                    )

            self.assertIsNotNone(oracle_min)
            self.assertIsNotNone(bracket["lower_bps"])
            self.assertIsNotNone(bracket["upper_bps"])
            self.assertLess(bracket["lower_bps"], bracket["upper_bps"])
            # The upper bound must meet or exceed min_vmaf
            self.assertGreaterEqual(bracket["upper_gate_score"], target_vmaf)
            # The lower bound must be below min_vmaf
            self.assertLess(bracket["lower_gate_score"], target_vmaf)
            # Oracle min must equal upper_bps (grid approximation, not interpolated)
            self.assertEqual(oracle_min, bracket["upper_bps"])
            self.assertLessEqual(bracket["refinement_evaluations"], 3)
            self.assertIn(oracle_min, [pt["bitrate_bps"] for pt in bracket["grid"]])

    def test_monotonic_bracket_with_empirical_noise(self) -> None:
        """When empirical points have non-monotonic dips, enforce monotonic lower/upper bounds."""
        eval_cache: dict[int, OraclePoint] = {
            500_000: OraclePoint(500_000, 88.0, 88.0, 88.0, False, 100_000),
            1_000_000: OraclePoint(1_000_000, 95.2, 95.2, 95.2, True, 200_000),   # Pass
            1_200_000: OraclePoint(1_200_000, 94.8, 94.8, 94.8, False, 240_000),  # Dip below 95.0
            1_400_000: OraclePoint(1_400_000, 96.0, 96.0, 96.0, True, 280_000),   # Stable pass
        }
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            item = self._sample_item(temp_dir)

            with patch("scripts.run_smart_case.build_encode_commands", return_value=([], None)), \
                 patch("scripts.run_smart_case.subprocess.run", return_value=MagicMock(returncode=0)), \
                 patch.object(Path, "is_file", return_value=True), \
                 patch.object(Path, "stat", return_value=MagicMock(st_size=500_000)):

                oracle_min, bracket = run_oracle_search(
                    ffmpeg_path=Path("/bin/ffmpeg"),
                    item=item,
                    workdir=temp_dir,
                    target_min_vmaf=95.0,
                    fps=30.0,
                    model_spec=VMAF_STANDARD_MODEL,
                    encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
                    evaluated_cache=eval_cache,
                    executed_commands=[],
                    max_coarse_points=0,  # Only use pre-seeded points
                    max_refinements=0,
                )

        self.assertEqual(bracket["lower_bps"], 500_000)
        self.assertEqual(bracket["upper_bps"], 1_000_000)
        self.assertEqual(oracle_min, 1_000_000)
        self.assertTrue(bracket["monotonicity_violated"])

    def test_oracle_all_points_failing(self) -> None:
        """If quality is unreachable, upper_bps and oracle_min must be None."""
        eval_cache: dict[int, OraclePoint] = {
            500_000: OraclePoint(500_000, 80.0, 80.0, 80.0, False, 100_000),
            1_000_000: OraclePoint(1_000_000, 85.0, 85.0, 85.0, False, 200_000),
            1_500_000: OraclePoint(1_500_000, 90.0, 90.0, 90.0, False, 300_000),
        }
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            item = self._sample_item(temp_dir)

            oracle_min, bracket = run_oracle_search(
                ffmpeg_path=Path("/bin/ffmpeg"),
                item=item,
                workdir=temp_dir,
                target_min_vmaf=95.0,
                fps=30.0,
                model_spec=VMAF_STANDARD_MODEL,
                encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
                evaluated_cache=eval_cache,
                executed_commands=[],
                max_coarse_points=0,
                max_refinements=0,
            )

        self.assertIsNone(oracle_min)
        self.assertIsNone(bracket["upper_bps"])
        self.assertEqual(bracket["lower_bps"], 1_500_000)

    def test_oracle_all_points_passing(self) -> None:
        """If even lowest bitrate passes, lower_bps is None and upper_bps is the lowest point."""
        eval_cache: dict[int, OraclePoint] = {
            500_000: OraclePoint(500_000, 96.0, 96.0, 96.0, True, 100_000),
            1_000_000: OraclePoint(1_000_000, 98.0, 98.0, 98.0, True, 200_000),
        }
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            item = self._sample_item(temp_dir)

            oracle_min, bracket = run_oracle_search(
                ffmpeg_path=Path("/bin/ffmpeg"),
                item=item,
                workdir=temp_dir,
                target_min_vmaf=95.0,
                fps=30.0,
                model_spec=VMAF_STANDARD_MODEL,
                encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
                evaluated_cache=eval_cache,
                executed_commands=[],
                max_coarse_points=0,
                max_refinements=0,
            )

        self.assertEqual(oracle_min, 500_000)
        self.assertEqual(bracket["upper_bps"], 500_000)
        self.assertIsNone(bracket["lower_bps"])


class TestMissingGroundTruthAndContract(unittest.TestCase):
    """Tests for missing ground-truth observations and size-blocked candidate handling."""

    def test_size_blocked_with_quality_candidate_evaluates_false_size_block(self) -> None:
        # Candidate passed quality (min_vmaf=96.0 >= 95.0), but predicted output exceeded max_output_bytes
        # If actual full encode size is 950_000 (<= 1_000_000), it's a false size block
        self.assertTrue(
            is_false_size_block(
                smart_size_blocked=True,
                full_encode_output_bytes=950_000,
                max_output_bytes=1_000_000,
            )
        )
        # If actual size is 1_050_000 (> 1_000_000), it was NOT a false size block
        self.assertFalse(
            is_false_size_block(
                smart_size_blocked=True,
                full_encode_output_bytes=1_050_000,
                max_output_bytes=1_000_000,
            )
        )

    def test_no_quality_candidate_produces_null_ground_truth_without_inventing_denominators(self) -> None:
        """When bound encoder cannot reach target VMAF, do not fabricate ground truth."""
        measurement = {
            "analysis_wall_seconds": 15.0,
            "final_encode_wall_seconds": None,
            "smart_passed": False,
            "ground_truth_passed": None,
            "smart_size_blocked": False,
            "full_encode_output_bytes": None,
            "max_output_bytes": 1_000_000,
            "selected_video_bitrate_bps": None,
            "oracle_minimum_bitrate_bps": None,
            "counts": {
                "scout_windows": 8,
                "quality_encodes": 4,
                "vmaf_measurements": 4,
                "holdout_measurements": 0,
                "size_calibration_encodes": 0,
            },
        }
        metrics = calculate_case_metrics(measurement)
        self.assertIsNone(metrics.analysis_cost)
        self.assertIsNone(metrics.quality_false_pass)
        self.assertIsNone(metrics.false_size_block)
        self.assertIsNone(metrics.bitrate_regret_absolute_bps)
        self.assertIsNone(metrics.bitrate_regret_normalized)

    def test_quality_false_pass_detection_behavior(self) -> None:
        # Smart claimed PASS, but ground truth failed -> False Pass
        self.assertTrue(is_quality_false_pass(smart_passed=True, ground_truth_passed=False))
        # Smart claimed PASS, ground truth passed -> True Pass
        self.assertFalse(is_quality_false_pass(smart_passed=True, ground_truth_passed=True))
        # Smart did not pass -> cannot be a false pass
        self.assertFalse(is_quality_false_pass(smart_passed=False, ground_truth_passed=False))


class TestCFRValidationAndVFRHandling(unittest.TestCase):
    """Tests for CFR validation and fail-clear behavior on VFR input."""

    def test_vfr_fails_clear_with_error(self) -> None:
        # Mock ffprobe stream with mismatched frame rates
        fake_payload = {
            "streams": [
                {
                    "r_frame_rate": "30/1",
                    "avg_frame_rate": "24/1",
                }
            ]
        }
        with patch("scripts.run_smart_case.subprocess.run") as mock_run:
            mock_run.side_effect = [MagicMock(returncode=0, stdout=json.dumps(fake_payload), stderr=""),
                                   MagicMock(returncode=0, stdout=json.dumps({"frames": [
                                       {"best_effort_timestamp_time": t} for t in (0, 0.033333, 0.066666)
                                   ]}), stderr="")]
            with self.assertRaisesRegex(RuntimeError, "VFR detected; temporal oracle fails clear"):
                validate_constant_fps(Path("/bin/ffprobe"), Path("/fake/vfr_video.mp4"))

    def test_cfr_matches_and_returns_fps(self) -> None:
        fake_payload = {
            "streams": [
                {
                    "r_frame_rate": "30000/1001",
                    "avg_frame_rate": "30000/1001",
                }
            ]
        }
        with patch("scripts.run_smart_case.subprocess.run") as mock_run:
            # First call for stream metadata, second call for read_intervals empty/equal
            mock_run.side_effect = [MagicMock(returncode=0, stdout=json.dumps(fake_payload), stderr=""),
                                   MagicMock(returncode=0, stdout=json.dumps({"frames": [
                                       {"best_effort_timestamp_time": t} for t in (0, 0.033367, 0.066733)
                                   ]}), stderr="")]
            fps, frame_count = validate_constant_fps(Path("/bin/ffprobe"), Path("/fake/cfr_video.mp4"))
            self.assertAlmostEqual(fps, 29.970, places=2)
            self.assertEqual(frame_count, 3)


class TestFullFrameGroundTruth(unittest.TestCase):
    def test_segmented_metric_merges_overlap_without_duplicate_frames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td)
            source = workdir / "source.mkv"
            distorted = workdir / "distorted.mp4"
            source.write_bytes(b"source")
            distorted.write_bytes(b"distorted")
            scores = [{"metrics": {"vmaf": 95.0}}] * 60

            def fake_run(command: list[str], **kwargs: object) -> MagicMock:
                # FFmpeg resolves a relative log_path against its working directory,
                # which the runner sets to the workdir.
                name = command[command.index("-filter_complex") + 1].split("log_path='")[1].split("'")[0]
                cwd = kwargs["cwd"]
                assert isinstance(cwd, Path)
                (cwd / name).write_text(json.dumps({"frames": scores}), encoding="utf-8")
                return MagicMock(returncode=0, stderr="")

            with patch("scripts.run_smart_case.subprocess.run", side_effect=fake_run):
                self.assertEqual(compute_segmented_vmaf_metrics(Path("ffmpeg"), distorted, source, VMAF_STANDARD_MODEL, VmafEncodeMetadata(1920,1080,8), 30.0, 90, workdir, segment_duration_sec=2, overlap_sec=1), (95.0, 95.0, 95.0))

    def test_segmented_vmaf_asks_for_a_bare_log_name(self) -> None:
        # FFmpeg consumes a backslash inside a quoted filter value, so a Windows
        # workdir path ("D:\a\...") would be written somewhere else if it were
        # embedded. The runner asks for a relative name and sets cwd=workdir.
        commands: list[list[str]] = []

        def fake_run(command: list[str], **_kwargs: object) -> MagicMock:
            commands.append(command)
            return MagicMock(returncode=1, stderr="stop after the first segment")

        with tempfile.TemporaryDirectory() as td, patch(
            "scripts.run_smart_case.subprocess.run", side_effect=fake_run
        ), self.assertRaisesRegex(RuntimeError, "Segmented VMAF failed"):
            workdir = Path(td).resolve()
            compute_segmented_vmaf_metrics(
                Path("ffmpeg"), workdir / "d.mp4", workdir / "s.mkv",
                VMAF_STANDARD_MODEL, VmafEncodeMetadata(1920, 1080, 8), 30.0, 90, workdir,
                segment_duration_sec=2, overlap_sec=1,
            )
        graph = commands[0][commands[0].index("-filter_complex") + 1]
        log_name = graph.split("log_path='")[1].split("'")[0]
        self.assertEqual(log_name, "seg-vmaf-000000000.json")
        self.assertNotIn(str(workdir), graph)

    def test_windows_completeness_probe_uses_sibling_executable(self) -> None:
        with patch("scripts.run_smart_case.subprocess.run", return_value=MagicMock(
            stdout=json.dumps({"streams": [{"nb_read_frames": "60"}]}),
        )) as run:
            require_complete_encode(Path("tools/ffmpeg.exe"), Path("output.mp4"), 60)
        self.assertEqual(run.call_args.args[0][0], str(Path("tools/ffprobe.exe")))

    def test_cfr_alignment_pairs_both_inputs_by_frame_index(self) -> None:
        command = ["ffmpeg", "-filter_complex", "[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b]", "-"]
        aligned = align_cfr_command(command, 30.0)
        self.assertEqual(aligned[aligned.index("-filter_complex") + 1], "[0:v]setpts=N/(30*TB)[a];[1:v]setpts=N/(30*TB)[b]")
        self.assertIn("PTS-STARTPTS", command[2])

    def test_truncated_ground_truth_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scores.json"
            path.write_text(json.dumps({"frames": [dict(frameNum=i, metrics=dict(vmaf=95)) for i in range(30)]}))
            with self.assertRaisesRegex(RuntimeError, "complete source"):
                compute_full_vmaf_metrics(path, VMAF_STANDARD_MODEL, 30.0, expected_frames=60)

    def test_worst_second_includes_interior_and_uses_full_windows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scores.json"
            scores = [100.0] * 60 + [70.0] * 30 + [100.0] * 59 + [0.0]
            path.write_text(json.dumps({"frames": [dict(frameNum=i, metrics=dict(vmaf=s))
                                                     for i, s in enumerate(scores)]}))
            mean, worst, gate = compute_full_vmaf_metrics(path, VMAF_STANDARD_MODEL, 30.0)
            self.assertAlmostEqual(mean, sum(scores) / len(scores))
            self.assertEqual(worst, 70.0)
            self.assertEqual(gate, 74.0)

    def test_subsampled_ground_truth_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "scores.json"
            path.write_text(json.dumps({"frames": [dict(frameNum=i, metrics=dict(vmaf=95)) for i in (0, 3, 6)]}))
            with self.assertRaisesRegex(RuntimeError, "contiguous"):
                compute_full_vmaf_metrics(path, VMAF_STANDARD_MODEL, 30.0)

    def test_missing_frame_timestamps_are_not_silently_accepted(self) -> None:
        payload = {"streams": [{"r_frame_rate": "30/1", "avg_frame_rate": "30/1"}]}
        with patch("scripts.run_smart_case.subprocess.run", return_value=MagicMock(
            returncode=0, stdout=json.dumps(payload), stderr="",
        )):
            with self.assertRaisesRegex(RuntimeError, "timestamps"):
                validate_constant_fps(Path("ffprobe"), Path("source"))


if __name__ == "__main__":
    unittest.main()
