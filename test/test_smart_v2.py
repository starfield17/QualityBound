from __future__ import annotations

import itertools
from array import array
import io
import json
import math
import os
from pathlib import Path
import random
import tempfile
import sys
import time
import unittest
from unittest.mock import patch

from core.models import (
    AnalysisProfileName, AudioMode, BackendChoice, CodecChoice, DecodeAcceleration, DecisionActionCode, DecisionOption, EncodeOptions, EncodePlanItem,
    EncodeResult, EncoderInfo, MediaInfo, OperationCancelledError, QualitySearchStatus,
    SegmentedAnalysisResult, ShotAnalysis, ShotCandidate, ShotRange, SmartAlgorithm,
    ConstraintFailureKind, SizeBlockedPolicy, SkipOrigin,
)
from core.config.store import encode_options_to_preset_data, preset_data_to_encode_options
from core.encoding.segmented import _validate_auxiliary, analyze_segmented_plan_item, execute_segmented_item, validate_scores
from core.smart.v1.decisions import prepare_size_miss_retry, reselect_after_quality_decision
from core.smart.v2.optimizer import SETTINGS, allocate, sample_windows, worst_one_second
from core.smart.v2.receipts import fingerprint, load, receipt_root, save
from core.smart.v2.runtime import Runtime, UnsupportedV2, decoder_configuration
from core.smart.v2.workflow import preflight, reselect


def candidate(rate: int, quality: float, size: int, worst: float | None = None) -> ShotCandidate:
    return ShotCandidate(rate, quality, quality if worst is None else worst, size, 30)


class V2OptimizerTests(unittest.TestCase):
    def test_v1_stays_default(self) -> None:
        self.assertEqual(EncodeOptions().smart_algorithm, SmartAlgorithm.V1)

    def test_matches_exhaustive_discrete_search(self) -> None:
        shots = [ShotAnalysis(ShotRange(0, 30), candidates=[candidate(1, 86, 10), candidate(2, 94, 20)]),
                 ShotAnalysis(ShotRange(30, 120), candidates=[candidate(1, 88, 11), candidate(2, 92, 30)])]
        expected = min(sum(c.predicted_video_bytes for c in pair)
                       for pair in itertools.product(*(s.candidates for s in shots))
                       if sum(c.mean_vmaf * s.shot.frame_count for c, s in zip(pair, shots)) / 120 >= 90)
        allocation = allocate(shots, 90, 100, 100)
        assert allocation is not None
        self.assertEqual(allocation.video_bytes, expected)
        self.assertGreaterEqual(allocation.mean_vmaf, 90)
        self.assertFalse(allocation.approximate)

    def test_local_floor_cannot_be_paid_for_with_high_quality(self) -> None:
        shots = [ShotAnalysis(ShotRange(0, 30), candidates=[candidate(1, 85, 1)]),
                 ShotAnalysis(ShotRange(30, 120), candidates=[candidate(1, 100, 1)])]
        self.assertIsNone(allocate(shots, 90, 100, 100))
        shots[0].candidates = [candidate(1, 90, 1, 81)]
        self.assertIsNone(allocate(shots, 90, 100, 100))

    # F1 ← S1
    def test_samples_leave_independent_nonoverlapping_holdout(self) -> None:
        windows, holdout = sample_windows(ShotRange(40, 940), 30, SETTINGS[AnalysisProfileName.BALANCE])
        assert holdout is not None
        self.assertEqual(len(windows), 2)
        self.assertTrue(all(w.end_frame <= holdout.start_frame for w in windows))
        self.assertEqual(sample_windows(ShotRange(0, 40), 30, SETTINGS[AnalysisProfileName.BALANCE]),
                         ([ShotRange(0, 40)], None))

    def test_boundary_rolling_gate(self) -> None:
        self.assertEqual(worst_one_second([100] * 15 + [60] * 30 + [100] * 15, 30), 60)
        self.assertEqual(worst_one_second([80, 90], 30), 85)

    def test_compression_is_explicit_and_stays_feasible(self) -> None:
        shots = [ShotAnalysis(ShotRange(0, 30), candidates=[candidate(n, 86 + n, n) for n in range(1, 12)])]
        result = allocate(shots, 90, 100, 3)
        assert result is not None
        self.assertTrue(result.approximate)
        self.assertGreaterEqual(result.mean_vmaf, 90)
        self.assertLessEqual(result.video_bytes, 100)

    def test_budget_failure_does_not_invent_candidates(self) -> None:
        shots = [ShotAnalysis(ShotRange(0, 30), candidates=[candidate(1, 94, 20)])]
        self.assertIsNone(allocate(shots, 90, 19, 20))

    def test_random_small_trellis_matches_exhaustive_oracle(self) -> None:
        rng = random.Random(982451653)
        for _ in range(100):
            shots, offset = [], 0
            for _ in range(3):
                count = rng.randrange(1, 100)
                choices = [candidate(i, rng.randrange(84, 100), rng.randrange(1, 40), rng.randrange(80, 99)) for i in range(4)]
                shots.append(ShotAnalysis(ShotRange(offset, offset + count), candidates=choices))
                offset += count
            combinations = [pair for pair in itertools.product(*(s.candidates for s in shots))
                            if all(c.mean_vmaf >= 86 and c.worst_1s_vmaf >= 82 for c in pair)
                            and sum(c.mean_vmaf * s.shot.frame_count for c, s in zip(pair, shots)) / offset >= 90
                            and sum(c.predicted_video_bytes for c in pair) <= 70]
            actual = allocate(shots, 90, 70, 1000)
            if not combinations:
                self.assertIsNone(actual)
            else:
                assert actual is not None
                self.assertEqual(actual.video_bytes, min(sum(c.predicted_video_bytes for c in pair) for pair in combinations))

    def test_failed_compressed_search_records_approximation(self) -> None:
        diagnostics: dict[str, bool] = {}
        shots = [ShotAnalysis(ShotRange(0, 30), candidates=[candidate(i, 96 + i / 100, i) for i in range(1, 20)])]
        self.assertIsNone(allocate(shots, 99, 100, 2, diagnostics=diagnostics))
        self.assertTrue(diagnostics["approximate"])


def plan(root: Path) -> EncodePlanItem:
    source = root / "source.mkv"
    source.write_bytes(b"x" * 1000)
    return EncodePlanItem(source, root / "output.mp4",
                          MediaInfo(source, 2, 4000, 4000, 0, 320, 180, 30, "ffv1", None),
                          EncoderInfo(CodecChoice.HEVC, BackendChoice.CPU, "libx265", True, "ultrafast"),
                          EncodeOptions(smart_algorithm=SmartAlgorithm.V2_EXPERIMENTAL, min_vmaf=90,
                                        max_output_ratio=0.5, min_video_kbps=1, analysis_profile=AnalysisProfileName.FAST,
                                        overwrite=True, copy_external_subtitles=False), ffprobe_path=root / "ffprobe")


def analysis() -> SegmentedAnalysisResult:
    shot = ShotRange(0, 60)
    point = candidate(100000, 94, 10)
    point.measured_frames = 60
    point.whole_shot = point.holdout_verified = point.full_verified = True
    return SegmentedAnalysisResult(QualitySearchStatus.FOUND, "libx265", BackendChoice.CPU,
                                   shots=[ShotAnalysis(shot, [shot], candidates=[point])], selected=[point],
                                   fps=30, fps_rational="30", source_frames=60, measurement_fingerprint="a" * 64)


class V2ContractTests(unittest.TestCase):
    def test_analysis_oversize_threshold_matches_v1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for ratio in (None, 1.0, 1.01):
                with self.subTest(ratio=ratio):
                    item = plan(root)
                    item.options.size_blocked_policy = SizeBlockedPolicy.ASK
                    quality = SegmentedAnalysisResult(
                        QualitySearchStatus.CONSTRAINT_UNSATISFIED, "libx265", BackendChoice.CPU,
                        failure_kind=ConstraintFailureKind.SIZE_BLOCKED, required_output_ratio=ratio,
                    )
                    with patch("core.encoding.segmented.analyze_segmented_quality", return_value=quality):
                        result = analyze_segmented_plan_item(root / "ffmpeg", item, root)
                    assert result is not None
                    oversize = ratio is not None and ratio > 1.0
                    self.assertEqual(result.skipped, oversize)
                    self.assertEqual(result.needs_decision, not oversize)
                    self.assertEqual(result.skip_origin, SkipOrigin.SMART_PREDICTED_OVERSIZE if oversize else None)

    def test_old_and_new_presets_roundtrip(self) -> None:
        old = encode_options_to_preset_data(EncodeOptions())
        old.pop("smart_algorithm")
        self.assertEqual(preset_data_to_encode_options(old).smart_algorithm, SmartAlgorithm.V1)
        new = EncodeOptions(smart_algorithm=SmartAlgorithm.V2_EXPERIMENTAL)
        self.assertEqual(preset_data_to_encode_options(encode_options_to_preset_data(new)).smart_algorithm, new.smart_algorithm)

    # F2 ← S1, S3
    def test_measurement_identity_excludes_policy_includes_production_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = plan(Path(directory))
            tool = Path(directory) / "ffmpeg"
            tool.write_bytes(b"tool")
            item.ffprobe_path.write_bytes(b"probe")  # type: ignore[union-attr]
            key = fingerprint(tool, Path(directory) / "ffprobe", item)
            item.options.min_vmaf, item.options.max_output_ratio = 80, 0.9
            self.assertEqual(key, fingerprint(tool, Path(directory) / "ffprobe", item))
            item.options.two_pass = True
            self.assertNotEqual(key, fingerprint(tool, Path(directory) / "ffprobe", item))
            item.options.two_pass = False
            item.options.decode_acceleration = DecodeAcceleration.VIDEOTOOLBOX
            self.assertNotEqual(key, fingerprint(tool, Path(directory) / "ffprobe", item))

    def test_receipt_has_separate_space_and_rejects_overlap_or_changed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = receipt_root(Path(directory), "a" * 64)
            root.mkdir(parents=True)
            result = analysis()
            artifact = root / "artifact.mkv"
            artifact.write_bytes(b"candidate")
            result.selected[0].artifact = artifact
            save(root, result)
            restored = analysis()
            self.assertTrue(load(root, restored))
            self.assertEqual(restored.shots[0].candidates[0].artifact, artifact.resolve())
            artifact.write_bytes(b"tampered")
            self.assertTrue(load(root, restored))
            self.assertIsNone(restored.shots[0].candidates[0].artifact)
            data = json.loads((root / "receipt.json").read_text())
            data["shots"][0]["holdout_window"] = {"start_frame": 5, "end_frame": 10}
            (root / "receipt.json").write_text(json.dumps(data))
            self.assertFalse(load(root, analysis()))

    def test_audio_and_container_budget_counted_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = plan(Path(directory))
            item.source_path.write_bytes(b"x" * 100000)
            item.options.audio_mode, item.options.audio_bitrate = AudioMode.AAC, "8k"
            item.media_info.audio_stream_count = 1  # type: ignore[union-attr]
            quality = analysis()
            reselect(quality, item)
            self.assertEqual(quality.video_budget_bytes, 49000 - 2000)
            self.assertEqual(quality.predicted_output_bytes, math.ceil((10 + 2000) / 0.98))

    def test_size_retry_changes_combination_budget_without_changing_shot_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = plan(Path(directory))
            item.segmented_analysis_result = analysis()
            item.segmented_analysis_result.video_budget_bytes = 490
            item.options.max_video_kbps = 700
            result = EncodeResult(item.source_path, item.output_path, False, needs_decision=True,
                                  rejected_output_path=Path(directory) / "preserved.mp4", actual_output_bytes=600, allowed_output_bytes=500)
            prepare_size_miss_retry(item, result)
            self.assertLess(item.segmented_video_budget_bytes, 490)  # type: ignore[arg-type]
            self.assertEqual(item.options.max_video_kbps, 700)
            self.assertIsNone(item.segmented_analysis_result)

    def test_cross_boundary_guard_cannot_hide_in_short_shot_means(self) -> None:
        result = analysis()
        result.source_frames = 60
        result.shots = [ShotAnalysis(ShotRange(0, 30)), ShotAnalysis(ShotRange(30, 60))]
        scores = array("d", [100] * 15 + [65] * 30 + [100] * 15)
        failed = validate_scores(result, scores, 85)
        self.assertEqual(failed, [0, 1])
        self.assertEqual(result.final_boundary_worst_1s, [65])

    def test_relax_size_releases_corrected_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = plan(Path(directory))
            quality = analysis()
            quality.shots[0].candidates[0].predicted_video_bytes = 450
            item.segmented_video_budget_bytes = 400
            reselect(quality, item)
            self.assertFalse(quality.success)
            decision = DecisionOption(DecisionActionCode.RELAX_SIZE, suggested_value=0.5)
            reselect_after_quality_decision(Path("ffmpeg"), item, quality, decision)
            self.assertTrue(quality.success)
            self.assertIsNone(item.segmented_video_budget_bytes)

    def test_shifted_chapters_are_validated_on_normalized_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path("ffmpeg"), Path("ffprobe"), plan(Path(directory)), Path(directory), io.StringIO())
            original = {"streams": [], "chapters": [{"start_time": "10", "end_time": "12", "tags": {"title": "Chapter"}}]}
            final = {"streams": [], "chapters": [{"start_time": "0", "end_time": "2", "tags": {"title": "Chapter"}}]}
            with patch.object(runtime, "probe", side_effect=[original, final]):
                _validate_auxiliary(runtime, Path("output.mp4"), 10)

    # N5 ← S3
    def test_vfr_is_rejected_without_resampling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path("ffmpeg"), Path("ffprobe"), plan(Path(directory)), Path(directory), io.StringIO())
            payload = {"streams": [{"codec_type": "video", "r_frame_rate": "30/1"}],
                       "frames": [{"best_effort_timestamp_time": t} for t in (0, 1/30, 0.1)]}
            with patch.object(runtime, "probe", return_value=payload):
                with self.assertRaises(UnsupportedV2):
                    runtime.timeline(Path("source"))

    def test_preflight_cancellation_remains_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path("ffmpeg"), Path("ffprobe"), plan(Path(directory)), Path(directory), io.StringIO())
            with patch.object(runtime, "encode", side_effect=OperationCancelledError("cancel")):
                with self.assertRaises(OperationCancelledError):
                    preflight(runtime, analysis(), 1000, 2000)

    def test_silent_child_process_is_cancelled_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            started = time.monotonic()
            runtime = Runtime(Path("ffmpeg"), Path("ffprobe"), plan(Path(directory)), Path(directory), io.StringIO(),
                              cancel_check=lambda: time.monotonic() - started > 0.2)
            with self.assertRaises(OperationCancelledError):
                runtime.run([sys.executable, "-c", "import time; time.sleep(10)"], "silent fixture")
            self.assertLess(time.monotonic() - started, 3)

    def test_decoder_headers_ignore_encoder_sei_but_keep_parameter_changes(self) -> None:
        def stream(sei: bytes, sps: bytes = b"sps") -> dict:
            data = bytearray([1] + [0] * 21 + [4])
            for kind, payload in ((32, b"vps"), (33, sps), (34, b"pps"), (39, sei)):
                data += bytes([kind]) + b"\x00\x01" + len(payload).to_bytes(2, "big") + payload
            return {"codec_name": "hevc", "extradata": "\n00000000: " + data.hex() + "  ascii\n"}
        self.assertEqual(decoder_configuration(stream(b"rate1")), decoder_configuration(stream(b"rate2")))
        self.assertNotEqual(decoder_configuration(stream(b"rate1")), decoder_configuration(stream(b"rate1", b"changed")))
        with self.assertRaises(RuntimeError):
            decoder_configuration({"codec_name": "hevc", "extradata": "\n00000000: 01  ascii\n"})

    def test_unsupported_identity_revalidation_returns_item_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = plan(Path(directory))
            item.segmented_analysis_result = analysis()
            unsupported = SegmentedAnalysisResult(QualitySearchStatus.UNSUPPORTED, "libx265", BackendChoice.CPU,
                                                   reason="unsupported envelope")
            with patch("core.encoding.segmented.fingerprint", side_effect=ValueError("unsupported envelope")), \
                    patch("core.encoding.segmented.analyze_segmented_quality", return_value=unsupported):
                result = execute_segmented_item(Path("ffmpeg"), item, Path(directory), smart_analysis_validated=True)
            self.assertFalse(result.success)
            self.assertEqual(result.segmented_analysis_result.status, QualitySearchStatus.UNSUPPORTED)  # type: ignore[union-attr]


class V2PublicationTests(unittest.TestCase):
    def execute(self, root: Path, *, size: int = 100, score: float | list[float] = 94, cancel: bool = False,
                publish_error: bool = False, cancel_after_score: bool = False) -> tuple[EncodePlanItem, EncodeResult | None]:
        item = plan(root)
        item.segmented_analysis_result = analysis()
        receipt_root(root, "a" * 64).mkdir(parents=True)
        (receipt_root(root, "a" * 64) / "assets").mkdir()
        item.output_path.write_bytes(b"original")

        class FakeRuntime:
            def __init__(self, ffmpeg, ffprobe, item, root, log, **kwargs):
                self.root, self.item = root, item
                self.commands, self.times, self.cpu_times = [], {}, {}
                self.encodes = self.vmaf_calls = 0

            def check(self):
                if cancel or (cancel_after_score and self.vmaf_calls > 0):
                    raise OperationCancelledError("cancel")

            def encode(self, shot, rate, fps, origin, path):
                self.encodes += 1
                path.write_bytes(b"segment")

            def run(self, command, phase):
                self.commands.append(command)
                Path(command[-1]).write_bytes(b"x" * size)

            def stream_signature(self, path):
                return ("hevc", "Main", 320, 180)

            def timeline(self, path):
                return 30, "30", 0, 60

            def probe(self, path, **kwargs):
                return {"streams": [], "packets": [], "chapters": []}

            def score(self, path, shot, fps, origin):
                self.vmaf_calls += 1
                value = score[min(self.vmaf_calls - 1, len(score) - 1)] if isinstance(score, list) else score
                return array("d", [value] * 60)

            def video_bytes(self, path):
                return 10

            def emit(self, *args, **kwargs):
                pass

        with patch("core.encoding.segmented.Runtime", FakeRuntime), patch("core.encoding.segmented.fingerprint", return_value="a" * 64):
            if publish_error:
                with patch("core.encoding.segmented.os.replace", side_effect=OSError("publish denied")):
                    result = execute_segmented_item(Path("ffmpeg"), item, root, smart_analysis_validated=True)
            elif cancel or cancel_after_score:
                with self.assertRaises(OperationCancelledError):
                    execute_segmented_item(Path("ffmpeg"), item, root, smart_analysis_validated=True)
                return item, None
            else:
                result = execute_segmented_item(Path("ffmpeg"), item, root, smart_analysis_validated=True)
        return item, result

    # N4 ← S2
    def test_size_miss_preserves_file_and_does_not_replace_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, result = self.execute(Path(directory), size=600)
            assert result is not None and result.rejected_output_path is not None
            self.assertFalse(result.success)
            self.assertTrue(result.needs_decision)
            self.assertEqual(result.rejected_output_path.stat().st_size, 600)
            self.assertEqual(item.output_path.read_bytes(), b"original")

    def test_quality_failure_never_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, result = self.execute(Path(directory), score=80)
            assert result is not None
            self.assertFalse(result.success)
            self.assertEqual(item.output_path.read_bytes(), b"original")
            self.assertFalse(list(Path(directory).glob(".*smart-v2*")))

    def test_cancellation_cleans_unpublished_output(self) -> None:
        for late in (False, True):
            with self.subTest(after_complete_score=late), tempfile.TemporaryDirectory() as directory:
                item, _ = self.execute(Path(directory), cancel=not late, cancel_after_score=late)
                self.assertEqual(item.output_path.read_bytes(), b"original")
                self.assertFalse(list(Path(directory).glob(".*smart-v2*")))

    def test_publish_error_preserves_destination_and_cleans_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, result = self.execute(Path(directory), publish_error=True)
            assert result is not None
            assert result.error_message is not None
            self.assertIn("publish denied", result.error_message)
            self.assertEqual(item.output_path.read_bytes(), b"original")
            self.assertFalse(list(Path(directory).glob(".*smart-v2*")))

    def test_success_has_complete_measured_quality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, result = self.execute(Path(directory))
            assert result is not None
            self.assertTrue(result.success)
            self.assertEqual(item.segmented_analysis_result.final_mean_vmaf, 94)  # type: ignore[union-attr]
            self.assertEqual(item.output_path.stat().st_size, 100)

    def test_failed_complete_score_triggers_bounded_repair_and_rescore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item, result = self.execute(Path(directory), score=[80, 94, 94])
            assert result is not None and item.segmented_analysis_result is not None
            self.assertTrue(result.success)
            self.assertEqual(item.segmented_analysis_result.refinement_rounds, 1)
            self.assertEqual(item.segmented_analysis_result.vmaf_executions, 3)
            self.assertEqual(len(item.segmented_analysis_result.shots[0].candidates), 2)


class V2AdapterTests(unittest.TestCase):
    def test_cli_flag_and_legacy_default(self) -> None:
        from cli.cli_entry import _build_parser, _options_from_args
        with tempfile.TemporaryDirectory() as directory:
            parser = _build_parser()
            args = parser.parse_args(["encode", "source.mkv", "--smart-algorithm", "v2_experimental"])
            self.assertEqual(_options_from_args(args, Path(directory)).smart_algorithm, SmartAlgorithm.V2_EXPERIMENTAL)
            args = parser.parse_args(["encode", "source.mkv"])
            self.assertEqual(_options_from_args(args, Path(directory)).smart_algorithm, SmartAlgorithm.V1)

    def test_gui_algorithm_and_quality_semantics_roundtrip(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from core.i18n import get_translator
        from gui.encode_options_panel import EncodeOptionsPanel
        app = QApplication.instance() or QApplication([])
        panel = EncodeOptionsPanel(get_translator("en"), {})
        panel.apply_options(EncodeOptions(smart_algorithm=SmartAlgorithm.V2_EXPERIMENTAL))
        self.assertEqual(panel.read_options().smart_algorithm, SmartAlgorithm.V2_EXPERIMENTAL)
        self.assertIn("mean", panel.min_vmaf_label.text().lower())
        panel.apply_options(EncodeOptions())
        self.assertEqual(panel.read_options().smart_algorithm, SmartAlgorithm.V1)
        panel.close()
        self.assertIsNotNone(app)

    def test_whole_baseline_measures_minimum_before_bracket_refinement(self) -> None:
        from scripts.run_smart_v2_case import baseline
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = plan(root)
            item.options.max_video_kbps = 20

            class BaselineRuntime:
                def __init__(self):
                    self.item, self.root, self.ffmpeg = item, root, Path("ffmpeg")
                    self.encodes, self.rate = 0, 0

                def run(self, command, phase):
                    self.rate = int(command[command.index("-b:v") + 1])
                    Path(command[-1]).write_bytes(b"output")

            runtime = BaselineRuntime()
            with patch("scripts.run_smart_v2_case.metrics", side_effect=lambda *args: {
                "quality_passed": runtime.rate >= 10000, "bytes": runtime.rate,
            }):
                result = baseline(runtime, analysis(), two_pass=False, trials=9)  # type: ignore[arg-type]
            self.assertEqual(result["points"][0]["requested_bitrate_bps"], 1000)
            self.assertTrue(result["selected"]["quality_passed"])
            self.assertLess(result["selected"]["requested_bitrate_bps"], 20000)
