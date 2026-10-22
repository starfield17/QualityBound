from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.encoding import execute_plan_item
from core.models import BackendChoice, EncodeOptions, QualitySearchResult, QualitySearchStatus
from core.smart.v1.measurement import SMART_ERROR_TAIL_CHARS

from _smart_quality_fixtures import _item


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
