from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.cli_entry import run_cli
from core.models import (
    CodecChoice,
    CompressionMode,
    ConstraintPolicy,
    EncodeOptions,
    EncodePlan,
    EncodeResult,
    VmafViewingContext,
)
from core.config.store import encode_options_to_preset_data, preset_data_to_encode_options
from core.smart.v1.bitrate import resolve_max_output_ratio

from _smart_quality_fixtures import _item


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
                    "--lang",
                    "en",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
