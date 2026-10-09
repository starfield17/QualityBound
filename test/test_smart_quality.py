from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QComboBox

from cli.cli_entry import run_cli
from core.encoding import execute_plan_concurrent, execute_plan_item
from core.models import (
    AudioMode,
    BackendChoice,
    CodecChoice,
    CompressionMode,
    ConstraintPolicy,
    EncodeOptions,
    EncodePlan,
    EncodePlanItem,
    EncodeResult,
    EncoderInfo,
    MediaInfo,
    OperationCancelledError,
    QualityCandidateResult,
    QualitySearchResult,
    QualitySearchStatus,
    VmafBackend,
    VmafRuntimeSupport,
    VmafViewingContext,
)
from core.config.store import encode_options_to_preset_data, preset_data_to_encode_options
from core.smart.v1.measurement import SMART_ERROR_TAIL_CHARS, SmartCommandError
from core.smart.v1.workflow import analyze_quality, choose_smart_sample_windows
from core.smart.v1.bitrate import calculate_smart_bitrate_budget, reselect_from_candidates, resolve_max_output_ratio, search_bitrate_candidates
from core.smart.v1.cache import measurement_configuration_fingerprint, quality_configuration_fingerprint
from core.smart.v1.measurement import run_logged as _run_logged
from core.smart.v1.measurement import score_candidate as _score_candidate
from gui.gui_mainwindow import MainWindow
from gui.queue_state import QueueItemStatus, create_queue_records


def _combo_item(combo: QComboBox, index: int) -> QStandardItem:
    """Return a combo box's item, which the panel builds from a standard model."""

    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    item = model.item(index)
    assert item is not None
    return item


def _media(path: Path, *, duration: float = 60.0, audio_streams: int = 1) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=duration,
        format_bitrate_bps=4_000_000,
        video_bitrate_bps=3_000_000,
        audio_bitrate_bps=128_000 * audio_streams,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        audio_stream_count=audio_streams,
        pix_fmt="yuv420p",
        color_transfer="bt709",
    )


def _item(source: Path, output: Path, options: EncodeOptions) -> EncodePlanItem:
    return EncodePlanItem(
        source_path=source,
        output_path=output,
        media_info=_media(source),
        encoder_info=EncoderInfo(
            codec=options.codec,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="slow",
        ),
        options=options,
    )


class SmartConfigurationTestCase(unittest.TestCase):
    def test_codec_default_output_ratios(self) -> None:
        self.assertEqual(resolve_max_output_ratio(CodecChoice.HEVC, None), 0.70)
        self.assertEqual(resolve_max_output_ratio(CodecChoice.AV1, None), 0.50)

    def test_legacy_preset_loads_as_fixed_bitrate(self) -> None:
        data = encode_options_to_preset_data(EncodeOptions(compression_mode=CompressionMode.FIXED_BITRATE))
        data.pop("compression_mode")
        data.pop("min_vmaf")
        data.pop("max_output_ratio")
        restored = preset_data_to_encode_options(data)
        self.assertEqual(restored.compression_mode, CompressionMode.FIXED_BITRATE)
        self.assertEqual(restored.min_vmaf, 90.0)

    def test_new_options_default_to_smart(self) -> None:
        self.assertEqual(EncodeOptions().compression_mode, CompressionMode.SMART)
        self.assertEqual(EncodeOptions().min_vmaf, 90.0)

    def test_explicit_saved_vmaf_target_is_preserved(self) -> None:
        data = encode_options_to_preset_data(EncodeOptions(min_vmaf=95.0))
        self.assertEqual(preset_data_to_encode_options(data).min_vmaf, 95.0)

    def test_viewing_context_preset_round_trip_and_legacy_default(self) -> None:
        data = encode_options_to_preset_data(
            EncodeOptions(viewing_context=VmafViewingContext.STANDARD_DISPLAY)
        )
        self.assertEqual(data["viewing_context"], "standard_display")
        self.assertEqual(
            preset_data_to_encode_options(data).viewing_context,
            VmafViewingContext.STANDARD_DISPLAY,
        )

        data.pop("viewing_context")
        self.assertEqual(
            preset_data_to_encode_options(data).viewing_context,
            VmafViewingContext.HIGH_FIDELITY,
        )

    def test_invalid_viewing_context_in_preset_is_rejected(self) -> None:
        data = encode_options_to_preset_data(EncodeOptions())
        data["viewing_context"] = "mobile"
        with self.assertRaises(ValueError):
            preset_data_to_encode_options(data)

    def test_cli_rejects_fixed_ratio_flag_in_smart_mode(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = run_cli(
                [
                    "plan",
                    "missing.mov",
                    "--compression-mode",
                    "smart",
                    "--ratio",
                    "0.5",
                ]
            )
        self.assertEqual(exit_code, 2)
        self.assertIn("--ratio cannot be used", stderr.getvalue())

    def test_cli_accepts_viewing_context_for_smart_and_rejects_it_for_fixed(self) -> None:
        from cli.cli_entry import _build_parser, _merge_options, _validate_compression_options

        smart_args = _build_parser().parse_args(
            ["plan", "missing.mov", "--viewing-context", "standard_display"]
        )
        smart_options = _merge_options(EncodeOptions(), smart_args)
        self.assertEqual(smart_options.viewing_context, VmafViewingContext.STANDARD_DISPLAY)
        _validate_compression_options(smart_options, smart_args)

        fixed_args = _build_parser().parse_args(
            [
                "plan",
                "missing.mov",
                "--compression-mode",
                "fixed_bitrate",
                "--viewing-context",
                "standard_display",
            ]
        )
        fixed_options = _merge_options(EncodeOptions(), fixed_args)
        with self.assertRaisesRegex(ValueError, "--viewing-context"):
            _validate_compression_options(fixed_options, fixed_args)

    def test_cli_returns_three_when_a_constraint_needs_a_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            item = _item(source, root / "output.mp4", EncodeOptions())
            plan = EncodePlan(
                items=[item],
                ffmpeg_path=root / "ffmpeg",
                ffprobe_path=root / "ffprobe",
                input_root=root,
                output_root=root,
            )
            result = EncodeResult(
                source_path=source,
                output_path=item.output_path,
                success=False,
                needs_decision=True,
                error_message="quality and size conflict",
            )
            with (
                patch("cli.cli_entry.build_encode_plan", return_value=plan),
                patch("cli.cli_entry.execute_plan", return_value=[result]) as execute,
                patch("cli.cli_entry.print_plan"),
                patch("cli.cli_entry.print_encode_results"),
            ):
                exit_code = run_cli(["encode", str(source), "--lang", "en"])

            self.assertEqual(exit_code, 3)
            self.assertEqual(execute.call_args.kwargs["constraint_policy"], ConstraintPolicy.RELAX_SIZE)

    def test_cli_returns_two_for_failed_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            item = _item(source, root / "output.mp4", EncodeOptions())
            plan = EncodePlan(
                items=[item],
                ffmpeg_path=root / "ffmpeg",
                ffprobe_path=root / "ffprobe",
                input_root=root,
                output_root=root,
            )
            result = EncodeResult(
                source_path=source,
                output_path=item.output_path,
                success=False,
                skipped=False,
                error_message="libvmaf unavailable",
            )
            with (
                patch("cli.cli_entry.build_encode_plan", return_value=plan),
                patch("cli.cli_entry.execute_plan", return_value=[result]),
                patch("cli.cli_entry.print_plan"),
                patch("cli.cli_entry.print_encode_results"),
            ):
                exit_code = run_cli(["encode", str(source), "--lang", "en"])

            self.assertEqual(exit_code, 2)
            self.assertFalse(item.output_path.exists())


class SmartSamplingAndBudgetTestCase(unittest.TestCase):
    def test_short_video_uses_one_full_window(self) -> None:
        windows = choose_smart_sample_windows(8.0)
        self.assertEqual([(window.start_sec, window.duration_sec) for window in windows], [(0.0, 8.0)])
        ten = choose_smart_sample_windows(10.0)
        self.assertEqual([(window.start_sec, window.duration_sec) for window in ten], [(0.0, 10.0)])

    def test_balance_window_budget_scales_with_video_duration(self) -> None:
        self.assertEqual(len(choose_smart_sample_windows(15.0)), 1)
        for duration, expected in ((30.0, 4), (7200.0, 6)):
            windows = choose_smart_sample_windows(duration)
            self.assertEqual(len(windows), expected)
            for window in windows:
                self.assertAlmostEqual(window.duration_sec, 5.0)
                self.assertGreaterEqual(window.start_sec, 0.0)
                self.assertLessEqual(window.start_sec + window.duration_sec, duration + 1e-9)
            for left, right in zip(windows, windows[1:]):
                self.assertLessEqual(left.start_sec + left.duration_sec, right.start_sec + 1e-9)

    # F1 ← S1
    def test_just_over_thirty_seconds_has_non_overlapping_windows(self) -> None:
        windows = choose_smart_sample_windows(31.0)
        self.assertEqual(len(windows), 4)
        for left, right in zip(windows, windows[1:]):
            self.assertLessEqual(left.start_sec + left.duration_sec, right.start_sec)

    def test_aac_budget_counts_each_audio_stream(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.mov"
            source.write_bytes(b"x" * 10_000_000)
            options = EncodeOptions(
                audio_mode=AudioMode.AAC,
                audio_bitrate="128k",
                max_output_ratio=0.70,
            )
            item = _item(source, Path(temp_dir) / "out.mp4", options)
            item.media_info = _media(source, audio_streams=2)
            budget = calculate_smart_bitrate_budget(item)
            self.assertEqual(budget.audio_bitrate_bps, 256_000)
            self.assertGreater(budget.max_video_bitrate_bps, 0)

    def test_missing_vmaf_is_reported_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"x" * 10_000_000)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "out.mp4", EncodeOptions())
            with patch(
                "core.smart.v1.workflow.select_vmaf_runtime",
                return_value=VmafRuntimeSupport(
                    VmafBackend.CPU, "vmaf_v1.0.16_3d0h", False, "missing libvmaf"
                ),
            ):
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")
            self.assertEqual(result.status, QualitySearchStatus.UNSUPPORTED)
            self.assertIn("missing libvmaf", result.reason or "")

    def test_unavailable_vmaf_filter_is_not_treated_as_a_runnable_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"x" * 10_000_000)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "out.mp4", EncodeOptions())
            with patch(
                "core.smart.v1.workflow.select_vmaf_runtime",
                return_value=VmafRuntimeSupport(
                    VmafBackend.CPU,
                    "vmaf_v1.0.16_3d0h",
                    False,
                    "libvmaf filter unavailable",
                ),
            ):
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")
            self.assertEqual(result.status, QualitySearchStatus.UNSUPPORTED)
            self.assertIn("filter unavailable", result.reason or "")

    def test_hdr_input_is_reported_as_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"x" * 10_000_000)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "out.mp4", EncodeOptions())
            assert item.media_info is not None
            item.media_info.color_transfer = "smpte2084"
            with patch(
                "core.smart.v1.workflow.select_vmaf_runtime",
                return_value=VmafRuntimeSupport(VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True),
            ):
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")
            self.assertEqual(result.status, QualitySearchStatus.UNSUPPORTED)
            self.assertIn("HDR", result.reason or "")


class SmartSearchTestCase(unittest.TestCase):
    def test_preferred_margin_is_soft_and_never_redefines_user_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            with source.open("wb") as fh:
                fh.truncate(100_000_000)
            item = _item(
                source,
                root / "out.mp4",
                EncodeOptions(min_vmaf=90.0, max_output_ratio=1.0),
            )
            candidates = [
                QualityCandidateResult(
                    video_bitrate_bps=1_600_000,
                    min_vmaf=90.2,
                    observed_video_bitrate_bps=1_600_000,
                ),
                QualityCandidateResult(
                    video_bitrate_bps=1_700_000,
                    min_vmaf=90.7,
                    observed_video_bitrate_bps=1_700_000,
                ),
            ]
            preferred = reselect_from_candidates(candidates, item)
            self.assertEqual(preferred.selected_video_bitrate_bps, 1_700_000)

            item.options = replace(item.options, max_output_ratio=0.155)
            fallback = reselect_from_candidates(candidates, item)
            self.assertEqual(fallback.status, QualitySearchStatus.FOUND)
            self.assertEqual(fallback.selected_video_bitrate_bps, 1_600_000)

    def test_search_selects_lowest_tested_passing_candidate(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 1_800_000 else 94.0,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
        )
        self.assertEqual(selected, min(item.video_bitrate_bps for item in candidates if item.min_vmaf >= 95.0))
        self.assertEqual(required, selected)
        self.assertLessEqual(len(tested), 8)
        self.assertEqual(len(tested), len(set(tested)))

    def test_non_monotonic_scores_still_choose_lowest_tested_pass(self) -> None:
        scores = {
            3_000_000: 96.0,
            500_000: 90.0,
            1_750_000: 95.5,
            1_125_000: 94.0,
            1_437_000: 95.2,
            1_281_000: 93.0,
        }

        def evaluate(bitrate: int) -> QualityCandidateResult:
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=scores.get(bitrate, 94.0))

        candidates, selected, _required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
        )
        passing = [candidate.video_bitrate_bps for candidate in candidates if candidate.min_vmaf >= 95.0]
        self.assertEqual(selected, min(passing))

    def test_failed_budget_candidate_estimates_required_bitrate_without_selecting_it(self) -> None:
        def evaluate(bitrate: int) -> QualityCandidateResult:
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 3_000_000 else 93.0,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=2_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
        )
        self.assertIsNone(selected)
        self.assertIsNotNone(required)
        self.assertGreater(required or 0, 2_000_000)
        self.assertIn(required, [candidate.video_bitrate_bps for candidate in candidates])

    def test_size_constraint_uses_measured_prediction_for_selection(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 1_800_000 else 94.0,
                predicted_output_bytes=bitrate // 1_000,
                predicted_output_ratio=bitrate / 10_000_000,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
            max_output_bytes=2_000,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected, min(candidate.video_bitrate_bps for candidate in candidates if candidate.min_vmaf >= 95.0 and (candidate.predicted_output_bytes or 0) <= 2_000))
        self.assertEqual(required, min(candidate.video_bitrate_bps for candidate in candidates if candidate.min_vmaf >= 95.0))
        self.assertLessEqual(len(tested), 8)

    def test_size_constraint_rejects_quality_passing_candidates(self) -> None:
        def evaluate(bitrate: int) -> QualityCandidateResult:
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 1_800_000 else 94.0,
                predicted_output_bytes=10_000,
                predicted_output_ratio=1.0,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
            max_output_bytes=2_000,
        )
        self.assertIsNone(selected)
        self.assertIsNotNone(required)
        required_candidate = next(candidate for candidate in candidates if candidate.video_bitrate_bps == required)
        self.assertEqual(required_candidate.predicted_output_ratio, 1.0)

    def test_search_never_tests_more_than_eight_unique_candidates(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=94.0)

        search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=1_000,
            budget_bitrate_bps=10_000_000,
            required_search_ceiling_bps=20_000_000,
            min_vmaf=95.0,
            max_output_bytes=1,
        )
        self.assertLessEqual(len(tested), 8)
        self.assertEqual(len(tested), len(set(tested)))

    def test_coarse_search_stops_at_four_candidates_and_wide_tolerance(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=94.0)

        from core.smart.v1.runtime import COARSE_MAX_CANDIDATES, search_tolerance_bps

        search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=1_000,
            budget_bitrate_bps=10_000_000,
            required_search_ceiling_bps=20_000_000,
            min_vmaf=95.0,
            max_candidates=COARSE_MAX_CANDIDATES,
            max_output_bytes=1,
            tolerance_bps=search_tolerance_bps(20_000_000),
        )
        self.assertLessEqual(len(tested), 4)
        self.assertGreaterEqual(search_tolerance_bps(1_000_000), 50_000)
        self.assertEqual(search_tolerance_bps(10_000_000), 300_000)


class SmartCommandAndMeasurementTestCase(unittest.TestCase):
    def test_smart_process_uses_devnull_stdin_and_preserves_process_kwargs(self) -> None:
        class SuccessfulProcess:
            stdout = iter(["ok\n"])

            def wait(self, *_args, **_kwargs) -> int:
                return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "smart.log"
            with (
                patch(
                    "core.smart.v1.measurement.hidden_popen_kwargs",
                    return_value={"creationflags": 0x08000000},
                ),
                patch("core.smart.v1.measurement.subprocess.Popen", return_value=SuccessfulProcess()) as popen,
                log_path.open("w", encoding="utf-8") as log_file,
            ):
                _run_logged(
                    ["ffmpeg", "-i", "input.mp4"],
                    log_file,
                    cancel_check=None,
                    process_callback=None,
                    cwd=root,
                    phase="reference extraction",
                )

        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["cwd"], root)
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_smart_process_start_oserror_is_flushed_to_log_with_phase(self) -> None:
        class FlushTrackingLog(io.StringIO):
            flush_count = 0

            def flush(self) -> None:
                self.flush_count += 1
                super().flush()

        os_error = "cannot launch ffmpeg: " + ("x" * (SMART_ERROR_TAIL_CHARS + 100))
        log_file = FlushTrackingLog()
        with (
            patch("core.smart.v1.measurement.subprocess.Popen", side_effect=OSError(os_error)),
            self.assertRaises(RuntimeError) as raised,
        ):
            _run_logged(
                ["ffmpeg", "-i", "input.mp4"],
                log_file,
                cancel_check=None,
                process_callback=None,
                phase="reference extraction",
            )

        contents = log_file.getvalue()
        self.assertIn("ffmpeg -i input.mp4", contents)
        self.assertIn("reference extraction", contents)
        self.assertIn("cannot launch ffmpeg", contents)
        self.assertIn("[smart process start failed]", contents)
        self.assertLessEqual(contents.count("x"), SMART_ERROR_TAIL_CHARS)
        self.assertGreaterEqual(log_file.flush_count, 2)
        self.assertIn("reference extraction", str(raised.exception))

    def test_smart_command_failure_identifies_phase_command_and_bounded_tail(self) -> None:
        class FailedProcess:
            stdout = iter(["initial output\n", "x" * (SMART_ERROR_TAIL_CHARS + 100) + "\n"])

            def wait(self, *_args, **_kwargs) -> int:
                return 17

            def terminate(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "smart.log"
            with (
                patch("core.smart.v1.measurement.subprocess.Popen", return_value=FailedProcess()),
                log_path.open("w", encoding="utf-8") as log_file,
                self.assertRaises(SmartCommandError) as raised,
            ):
                _run_logged(
                    ["ffmpeg", "-i", "input.mp4"],
                    log_file,
                    cancel_check=None,
                    process_callback=None,
                    phase="VMAF scoring",
                )
        self.assertIn("VMAF scoring", str(raised.exception))
        self.assertIn("exit code 17", str(raised.exception))
        self.assertIn("ffmpeg -i input.mp4", str(raised.exception))
        self.assertLessEqual(len(raised.exception.output_tail), SMART_ERROR_TAIL_CHARS)

    def test_vmaf_scoring_uses_relative_log_path_and_measured_sample_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 100_000)
            item = _item(source, root / "output.mp4", EncodeOptions())
            reference = root / "reference.mkv"
            reference.write_bytes(b"reference")
            log_path = root / "smart.log"
            log_path.write_text("", encoding="utf-8")
            score_calls: list[dict[str, object]] = []

            def fake_run(command: list[str], _log_file: object, **kwargs: object) -> None:
                phase = kwargs.get("phase")
                if phase == "candidate encode":
                    Path(command[-1]).write_bytes(b"c" * 1_000)
                elif phase == "VMAF scoring":
                    score_calls.append({"command": command, **kwargs})
                    filter_graph = command[command.index("-filter_complex") + 1]
                    match = re.search(r"log_path='([^']+)'", filter_graph)
                    self.assertIsNotNone(match)
                    json_name = match.group(1) if match else ""
                    self.assertEqual(Path(json_name).name, json_name)
                    self.assertNotIn(str(root), filter_graph)
                    cwd = kwargs["cwd"]
                    assert isinstance(cwd, Path)
                    (cwd / json_name).write_text(
                        json.dumps(
                            {
                                "pooled_metrics": {"vmaf": {"mean": 96.0}},
                                "frames": [
                                    {"frameNum": index, "metrics": {"vmaf": 96.0}}
                                    for index in range(300)
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )

            with (
                patch("core.smart.v1.measurement.run_logged", side_effect=fake_run),
                log_path.open("a", encoding="utf-8") as smart_log,
            ):
                result = _score_candidate(
                    Path("ffmpeg"),
                    item,
                    [reference],
                    1_000_000,
                    root,
                    root,
                    smart_log,
                    window_durations_sec=[10.0],
                    audio_bitrate_bps=128_000,
                    source_bytes=source.stat().st_size,
                    cancel_check=None,
                    process_callback=None,
                )

            self.assertEqual(len(score_calls), 1)
            score_command = score_calls[0]["command"]
            self.assertIsInstance(score_command, list)
            assert isinstance(score_command, list)
            self.assertEqual(score_command[score_command.index("-loglevel") + 1], "error")
            filter_graph = str(score_command[score_command.index("-filter_complex") + 1])
            self.assertIn("settb=AVTB,setpts=PTS-STARTPTS", filter_graph)
            self.assertIn("n_threads=", filter_graph)
            self.assertIn("n_subsample=1", filter_graph)
            self.assertEqual(result.encoded_bytes, [1_000])
            self.assertEqual(result.encoded_durations_sec, [10.0])
            self.assertEqual(result.segment_vmaf, [96.0])
            self.assertEqual(result.segment_mean_vmaf, [96.0])
            self.assertEqual(result.segment_p10_vmaf, [96.0])
            self.assertEqual(result.segment_worst_1s_vmaf, [96.0])
            self.assertEqual(result.segment_quality_scores, [96.0])
            self.assertEqual(result.observed_video_bitrate_bps, 800)
            self.assertIsNotNone(result.predicted_output_bytes)
            self.assertIsNotNone(result.predicted_output_ratio)


class SmartExecutionSafetyTestCase(unittest.TestCase):
    def _quality_result(self, max_bytes: int = 700) -> QualitySearchResult:
        return QualitySearchResult(
            status=QualitySearchStatus.FOUND,
            encoder_name="libx265",
            backend=BackendChoice.CPU,
            selected_video_bitrate_bps=500_000,
            min_vmaf=96.0,
            predicted_output_bytes=600,
            predicted_output_ratio=0.60,
            max_output_bytes=max_bytes,
            fingerprint="fingerprint",
        )

    def test_smart_failure_activity_message_points_to_log_without_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            item = _item(source, root / "output.mp4", EncodeOptions())
            activity_messages: list[str] = []
            long_error = "Smart reference extraction failed: " + ("x" * SMART_ERROR_TAIL_CHARS)

            with patch("core.encoding.analysis.analyze_quality", side_effect=RuntimeError(long_error)):
                result = execute_plan_item(
                    Path("ffmpeg"),
                    item,
                    root,
                    log_callback=activity_messages.append,
                )

        self.assertFalse(result.success)
        self.assertIsNotNone(result.log_path)
        failure_messages = [message for message in activity_messages if "Failed source.mov" in message]
        self.assertEqual(len(failure_messages), 1)
        self.assertIn(str(result.log_path), failure_messages[0])
        self.assertNotIn("x" * 100, failure_messages[0])

    # F2 ← S1, S3
    def test_analysis_from_a_different_encoder_is_never_published(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            item = _item(source, root / "output.mp4", EncodeOptions())
            mismatched = self._quality_result()
            mismatched.backend = BackendChoice.NVENC

            with (
                patch("core.encoding.analysis.analyze_quality", return_value=mismatched),
                patch("core.encoding.executor._run_logged_command") as run_command,
            ):
                result = execute_plan_item(Path("ffmpeg"), item, root)

            self.assertFalse(result.success)
            self.assertIn("different encoder", result.error_message or "")
            run_command.assert_not_called()

    def test_oversized_output_does_not_replace_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            output = root / "output.mp4"
            output.write_bytes(b"original")
            options = EncodeOptions(max_output_ratio=0.70, overwrite=True)
            item = _item(source, output, options)

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"x" * 800)

            with (
                patch("core.encoding.analysis.analyze_quality", return_value=self._quality_result()),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                result = execute_plan_item(Path("ffmpeg"), item, root)

            self.assertFalse(result.skipped)
            self.assertTrue(result.needs_decision)
            self.assertIsNotNone(result.rejected_output_path)
            assert result.rejected_output_path is not None
            self.assertEqual(result.rejected_output_path.read_bytes(), b"x" * 800)
            self.assertEqual(result.actual_output_bytes, 800)
            self.assertEqual(result.allowed_output_bytes, 700)
            self.assertEqual(result.effective_min_vmaf, options.min_vmaf)
            self.assertEqual(result.effective_max_output_ratio, 0.70)
            self.assertEqual(output.read_bytes(), b"original")
            self.assertFalse(list(root.glob(".*.partial-*")))

    def test_late_existing_target_is_preserved_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            output = root / "output.mp4"
            output.write_bytes(b"existing")
            item = _item(
                source,
                output,
                EncodeOptions(max_output_ratio=0.70, overwrite=False),
            )

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"x" * 600)

            with (
                patch("core.encoding.analysis.analyze_quality", return_value=self._quality_result()),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                result = execute_plan_item(Path("ffmpeg"), item, root)

            self.assertFalse(result.success)
            self.assertFalse(result.skipped)
            self.assertEqual(output.read_bytes(), b"existing")
            self.assertFalse(list(root.glob(".*.partial-*")))


class SmartParallelExecutionTestCase(unittest.TestCase):
    def _quality_result(self, max_bytes: int = 700) -> QualitySearchResult:
        return QualitySearchResult(
            status=QualitySearchStatus.FOUND,
            encoder_name="libx265",
            backend=BackendChoice.CPU,
            selected_video_bitrate_bps=500_000,
            min_vmaf=96.0,
            predicted_output_bytes=600,
            predicted_output_ratio=0.60,
            max_output_bytes=max_bytes,
            fingerprint="fingerprint",
        )

    def test_concurrent_workers_preserve_bindings_during_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            items: list[EncodePlanItem] = []
            for index in range(2):
                source = root / f"source-{index}.mov"
                source.write_bytes(b"s" * 1_000)
                item = _item(source, root / f"output-{index}.mp4", EncodeOptions(overwrite=True))
                if index == 1:
                    item.encoder_info = EncoderInfo(
                        codec=CodecChoice.HEVC,
                        backend=BackendChoice.NVENC,
                        encoder_name="hevc_nvenc",
                        supports_two_pass=False,
                        default_preset=None,
                    )
                    item.options = replace(item.options, backend=BackendChoice.NVENC)
                items.append(item)
            plan = EncodePlan(
                items=items,
                ffmpeg_path=Path("ffmpeg"),
                ffprobe_path=Path("ffprobe"),
                input_root=root,
                output_root=root,
            )
            active = 0
            max_active = 0
            observed_backends: set[BackendChoice] = set()
            counter_lock = threading.Lock()

            def fake_analysis(_ffmpeg, item, _workdir, _log_path, **_kwargs):
                nonlocal active, max_active
                self.assertIsNotNone(item.encoder_info)
                observed_backends.add(item.encoder_info.backend)
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with counter_lock:
                    active -= 1
                return QualitySearchResult(
                    status=QualitySearchStatus.FOUND,
                    encoder_name=item.encoder_info.encoder_name,
                    backend=item.encoder_info.backend,
                    selected_video_bitrate_bps=500_000,
                    min_vmaf=96.0,
                    predicted_output_ratio=0.60,
                    max_output_bytes=700,
                    fingerprint=item.encoder_info.backend.value,
                )

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"x" * 600)

            with (
                patch("core.encoding.analysis.analyze_quality", side_effect=fake_analysis),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                results = execute_plan_concurrent(plan, root, max_workers=2)

            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.success for result in results))
            self.assertEqual(observed_backends, {BackendChoice.CPU, BackendChoice.NVENC})
            self.assertGreaterEqual(max_active, 1)
            self.assertLessEqual(max_active, 2)

    def test_queue_analyzes_every_item_before_any_encode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            items: list[EncodePlanItem] = []
            for index in range(3):
                source = root / f"source-{index}.mov"
                source.write_bytes(b"s" * 1_000)
                items.append(_item(source, root / f"output-{index}.mp4", EncodeOptions(overwrite=True)))
            plan = EncodePlan(
                items=items,
                ffmpeg_path=Path("ffmpeg"),
                ffprobe_path=Path("ffprobe"),
                input_root=root,
                output_root=root,
            )
            events: list[str] = []

            def fake_analysis(_ffmpeg, item, _workdir, _log_path, **_kwargs):
                events.append(f"analyze:{item.source_path.name}")
                time.sleep(0.01)
                return self._quality_result()

            def fake_run(cmd, *_args, **_kwargs) -> None:
                events.append(f"encode:{Path(cmd[-1]).name}")
                Path(cmd[-1]).write_bytes(b"x" * 600)

            with (
                patch("core.encoding.analysis.analyze_quality", side_effect=fake_analysis),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                results = execute_plan_concurrent(plan, root, max_workers=2)

            self.assertEqual(len(results), 3)
            self.assertTrue(all(result.success for result in results))
            analyze_events = [event for event in events if event.startswith("analyze:")]
            encode_events = [event for event in events if event.startswith("encode:")]
            self.assertEqual(len(analyze_events), 3)
            self.assertTrue(encode_events)
            last_analyze = max(index for index, event in enumerate(events) if event.startswith("analyze:"))
            first_encode = min(index for index, event in enumerate(events) if event.startswith("encode:"))
            self.assertLess(last_analyze, first_encode)

    def test_successful_output_is_published_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            output = root / "output.mp4"
            options = EncodeOptions(max_output_ratio=0.70, overwrite=True)
            item = _item(source, output, options)

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"x" * 600)

            with (
                patch("core.encoding.analysis.analyze_quality", return_value=self._quality_result()),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                result = execute_plan_item(Path("ffmpeg"), item, root)

            self.assertTrue(result.success)
            self.assertEqual(output.stat().st_size, 600)

    def test_fingerprint_changes_when_backend_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "output.mp4", EncodeOptions())
            first = quality_configuration_fingerprint(ffmpeg, item)
            item.encoder_info = EncoderInfo(
                codec=CodecChoice.HEVC,
                backend=BackendChoice.NVENC,
                encoder_name="hevc_nvenc",
                supports_two_pass=False,
                default_preset="p6",
            )
            second = quality_configuration_fingerprint(ffmpeg, item)
            self.assertNotEqual(first, second)

    def test_search_controls_change_decision_but_not_measurement_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "output.mp4", EncodeOptions())
            measurement = measurement_configuration_fingerprint(ffmpeg, item)
            decision = quality_configuration_fingerprint(ffmpeg, item)

            item.options = replace(
                item.options,
                analysis_settings=replace(
                    item.options.analysis_settings,
                    exact_max_candidates=item.options.analysis_settings.exact_max_candidates + 1,
                    min_search_tolerance_bps=25_000,
                ),
            )

            self.assertEqual(measurement_configuration_fingerprint(ffmpeg, item), measurement)
            self.assertNotEqual(quality_configuration_fingerprint(ffmpeg, item), decision)

    def test_matching_analysis_cache_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "output.mp4", EncodeOptions())
            cached = self._quality_result()
            cached.fingerprint = quality_configuration_fingerprint(ffmpeg, item)
            item.quality_search_result = cached
            with patch(
                "core.smart.v1.workflow.select_vmaf_runtime",
                return_value=VmafRuntimeSupport(VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True),
            ) as detect:
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")
            self.assertIs(result, cached)
            detect.assert_called_once()

    def test_cancelled_output_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            item = _item(source, root / "output.mp4", EncodeOptions(overwrite=True))

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"partial")
                raise OperationCancelledError("cancel")

            with (
                patch("core.encoding.analysis.analyze_quality", return_value=self._quality_result()),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
                self.assertRaises(OperationCancelledError),
            ):
                execute_plan_item(Path("ffmpeg"), item, root)
            self.assertFalse(list(root.glob(".*.partial-*")))


class SmartGuiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def test_smart_controls_and_queue_initial_state(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            panel = window.options_panel
            smart_index = panel.compression_mode_combo.findData(CompressionMode.SMART.value)
            panel.compression_mode_combo.setCurrentIndex(smart_index)
            self.assertFalse(panel.ratio_edit.isEnabled())
            self.assertTrue(panel.min_vmaf_spin.isEnabled())
            self.assertEqual(panel.min_vmaf_spin.value(), 90.0)
            self.assertFalse(panel.viewing_context_combo.isHidden())
            self.assertEqual(
                panel.viewing_context_combo.currentData(),
                VmafViewingContext.HIGH_FIDELITY.value,
            )
            self.assertEqual(
                panel.viewing_context_combo.currentText(),
                "High-fidelity / Large display",
            )
            standard_index = panel.viewing_context_combo.findData(
                VmafViewingContext.STANDARD_DISPLAY.value
            )
            panel.viewing_context_combo.setCurrentIndex(standard_index)
            self.assertEqual(
                panel.read_options().viewing_context,
                VmafViewingContext.STANDARD_DISPLAY,
            )
            fixed_index = panel.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value)
            panel.compression_mode_combo.setCurrentIndex(fixed_index)
            self.assertTrue(panel.viewing_context_combo.isHidden())

            panel.apply_options(
                EncodeOptions(viewing_context=VmafViewingContext.STANDARD_DISPLAY)
            )
            self.assertEqual(
                panel.viewing_context_combo.currentData(),
                VmafViewingContext.STANDARD_DISPLAY.value,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                source = root / "source.mov"
                source.write_bytes(b"source")
                item = _item(source, root / "output.mp4", EncodeOptions())
                plan = EncodePlan(
                    items=[item],
                    ffmpeg_path=Path("ffmpeg"),
                    ffprobe_path=Path("ffprobe"),
                    input_root=root,
                    output_root=root,
                )
                records = create_queue_records(plan, root)
                self.assertEqual(records[0].status, QueueItemStatus.WAITING_ANALYSIS)
        finally:
            window.close()

    def test_unavailable_vmaf_disables_smart_mode(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                {
                    "codecs": {"hevc": [], "av1": []},
                    "vmaf": {
                        "runnable": False,
                        "model": "vmaf_v1.0.16_3d0h",
                        "backend": "cpu",
                        "error_message": "missing libvmaf",
                    },
                }
            )
            panel = window.options_panel
            smart_index = panel.compression_mode_combo.findData(CompressionMode.SMART.value)
            smart_item = _combo_item(panel.compression_mode_combo, smart_index)
            self.assertFalse(smart_item.isEnabled())
            self.assertEqual(
                panel.compression_mode_combo.currentData(),
                CompressionMode.FIXED_BITRATE.value,
            )
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
