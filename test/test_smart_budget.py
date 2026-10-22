from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.models import (
    AudioMode,
    EncodeOptions,
    QualitySearchStatus,
    VmafBackend,
    VmafRuntimeSupport,
)
from core.smart.v1.workflow import analyze_quality, choose_smart_sample_windows
from core.smart.v1.bitrate import calculate_smart_bitrate_budget

from _smart_quality_fixtures import _item, _media


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
