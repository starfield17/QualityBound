from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox

from cli.cli_entry import _build_parser, _normalize_auto_backend_preset, run_cli
from core.media.bitrate import DEFAULT_RATIO
from core.ffmpeg.commands import build_video_args
from core.ffmpeg.encoders import default_preset_for_encoder, is_valid_preset, preset_choices_for_encoder
from core.models import (
    BackendChoice,
    CodecChoice,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
    MediaInfo,
    VideoFileItem,
)
from core.encoding import build_encode_plan
from core.config.store import preset_data_to_encode_options
from gui.gui_mainwindow import MainWindow


def _media(path: Path) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=12.0,
        format_bitrate_bps=5_000_000,
        video_bitrate_bps=4_000_000,
        audio_bitrate_bps=128_000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
    )


def _encoder_info(encoder_name: str, backend: BackendChoice, default_preset: str | None) -> EncoderInfo:
    return EncoderInfo(
        codec=CodecChoice.HEVC,
        backend=backend,
        encoder_name=encoder_name,
        supports_two_pass=encoder_name == "libx265",
        default_preset=default_preset,
    )


def _capabilities_for(codec: CodecChoice, entries: list[tuple[BackendChoice, str]]) -> dict:
    return {
        "codecs": {
            codec.value: [
                {"backend": backend.value, "encoder": encoder_name, "preset_choices": []}
                for backend, encoder_name in entries
            ],
            (CodecChoice.AV1 if codec == CodecChoice.HEVC else CodecChoice.HEVC).value: [],
        }
    }


def _capabilities_by_codec(
    hevc: list[tuple[BackendChoice, str]],
    av1: list[tuple[BackendChoice, str]] | None = None,
) -> dict:
    presets = {
        "hevc_nvenc": ["p1", "p2", "p3"],
        "hevc_qsv": ["veryfast", "fast", "slow"],
        "libx265": ["fast", "medium", "slow"],
    }
    return {
        "codecs": {
            "hevc": [
                {
                    "backend": backend.value,
                    "encoder": encoder_name,
                    "preset_choices": presets.get(encoder_name, []),
                }
                for backend, encoder_name in hevc
            ],
            "av1": [
                {"backend": backend.value, "encoder": encoder_name, "preset_choices": []}
                for backend, encoder_name in (av1 or [])
            ],
        }
    }


def _backend_combo_items(window: MainWindow) -> list[str]:
    combo = window.options_panel.backend_combo
    return [str(combo.itemData(index)) for index in range(combo.count())]


def _select_combo_data(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def _plan_item(encoder_name: str, backend: BackendChoice, options: EncodeOptions | None = None) -> EncodePlanItem:
    current = options or EncodeOptions()
    source = Path("source.mp4")
    return EncodePlanItem(
        source_path=source,
        output_path=Path("output.mkv"),
        media_info=_media(source),
        encoder_info=_encoder_info(encoder_name, backend, current.encoder_preset),
        options=current,
        target_video_bitrate_bps=2_000_000,
    )


class EncoderCapsTestCase(unittest.TestCase):
    def test_default_presets_are_quality_tuned(self) -> None:
        self.assertEqual(default_preset_for_encoder("libx265"), "slow")
        self.assertEqual(default_preset_for_encoder("libsvtav1"), "5")
        self.assertEqual(default_preset_for_encoder("hevc_nvenc"), "p6")
        self.assertEqual(default_preset_for_encoder("av1_nvenc"), "p6")
        self.assertEqual(default_preset_for_encoder("hevc_qsv"), "slow")
        self.assertEqual(default_preset_for_encoder("av1_qsv"), "slow")

    def test_fallback_preset_lists_match_expected(self) -> None:
        ffmpeg_path = Path("fake_ffmpeg")
        with patch("core.ffmpeg.encoders._cached_runtime_preset_choices", return_value=()):
            self.assertEqual(
                preset_choices_for_encoder(ffmpeg_path, "libx265"),
                ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"],
            )
            self.assertEqual(preset_choices_for_encoder(ffmpeg_path, "hevc_nvenc"), ["p1", "p2", "p3", "p4", "p5", "p6", "p7"])
            self.assertEqual(preset_choices_for_encoder(ffmpeg_path, "av1_nvenc"), ["p1", "p2", "p3", "p4", "p5", "p6", "p7"])
            self.assertEqual(
                preset_choices_for_encoder(ffmpeg_path, "hevc_qsv"),
                ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
            )
            self.assertEqual(
                preset_choices_for_encoder(ffmpeg_path, "av1_qsv"),
                ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
            )
            self.assertEqual(preset_choices_for_encoder(ffmpeg_path, "libsvtav1"), [])

    def test_is_valid_preset_uses_detected_choices(self) -> None:
        ffmpeg_path = Path("fake_ffmpeg")
        with patch("core.ffmpeg.encoders._cached_runtime_preset_choices", return_value=("p5", "p6")):
            self.assertTrue(is_valid_preset(ffmpeg_path, "hevc_nvenc", "p6"))
            self.assertFalse(is_valid_preset(ffmpeg_path, "hevc_nvenc", "slow"))

    def test_runtime_preset_cache_tracks_ffmpeg_file_identity(self) -> None:
        from core.ffmpeg.encoders import _cached_runtime_preset_choices

        first_help = "-preset value\n  fast 1\n"
        second_help = "-preset value\n  slow 1\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg_path = Path(temp_dir) / "ffmpeg"
            ffmpeg_path.write_bytes(b"first")
            _cached_runtime_preset_choices.cache_clear()
            with patch("core.ffmpeg.encoders._run_encoder_help", side_effect=[first_help, second_help]) as help_probe:
                self.assertEqual(preset_choices_for_encoder(ffmpeg_path, "hevc_amf"), ["fast"])
                ffmpeg_path.write_bytes(b"second-binary")
                self.assertEqual(preset_choices_for_encoder(ffmpeg_path, "hevc_amf"), ["slow"])
            self.assertEqual(help_probe.call_count, 2)
            _cached_runtime_preset_choices.cache_clear()


class PlanningAndCommandTestCase(unittest.TestCase):
    def test_default_ratios_are_relaxed(self) -> None:
        self.assertEqual(DEFAULT_RATIO[CodecChoice.HEVC], 0.76)
        self.assertEqual(DEFAULT_RATIO[CodecChoice.AV1], 0.64)

    def test_build_encode_plan_injects_default_preset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source = temp_root / "sample.mp4"
            source.write_text("x", encoding="utf-8")
            options = EncodeOptions(copy_external_subtitles=False, overwrite=True)
            encoder_info = _encoder_info("hevc_nvenc", BackendChoice.NVENC, "p6")
            with (
                patch("core.encoding.planning.discover_ffmpeg_tools", return_value=(temp_root / "ffmpeg", temp_root / "ffprobe")),
                patch(
                    "core.encoding.planning.ensure_encoder_capabilities",
                    return_value=_capabilities_for(CodecChoice.HEVC, [(BackendChoice.NVENC, "hevc_nvenc")]),
                ),
                patch("core.encoding.planning.resolve_encoder", return_value=encoder_info),
                patch("core.encoding.planning.preset_choices_for_encoder", return_value=["p5", "p6"]),
                patch("core.encoding.planning.is_valid_preset", return_value=True),
                patch("core.encoding.planning.probe_media_info", return_value=_media(source)),
                patch("core.encoding.planning.validate_plan_item"),
            ):
                plan = build_encode_plan(
                    input_path=None,
                    options=options,
                    output_dir=temp_root / "out",
                    workdir=temp_root,
                    files=[VideoFileItem(path=source, relative_path=Path(source.name))],
                )
            self.assertEqual(plan.items[0].options.encoder_preset, "p6")

    def test_invalid_default_preset_falls_back_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            source = temp_root / "sample.mp4"
            source.write_text("x", encoding="utf-8")
            options = EncodeOptions(copy_external_subtitles=False, overwrite=True)
            encoder_info = _encoder_info("hevc_nvenc", BackendChoice.NVENC, "p6")
            with (
                patch("core.encoding.planning.discover_ffmpeg_tools", return_value=(temp_root / "ffmpeg", temp_root / "ffprobe")),
                patch(
                    "core.encoding.planning.ensure_encoder_capabilities",
                    return_value={
                        "hwaccels": [],
                        "codecs": {
                            "hevc": [
                                {
                                    "backend": "nvenc",
                                    "encoder": "hevc_nvenc",
                                    "preset_choices": ["p5"],
                                }
                            ],
                            "av1": [],
                        },
                    },
                ),
                patch("core.encoding.planning.resolve_encoder", return_value=encoder_info),
                patch("core.encoding.planning.preset_choices_for_encoder", return_value=["p5"]),
                patch("core.encoding.planning.is_valid_preset", return_value=False),
                patch("core.encoding.planning.probe_media_info", return_value=_media(source)),
                patch("core.encoding.planning.validate_plan_item"),
            ):
                plan = build_encode_plan(
                    input_path=None,
                    options=options,
                    output_dir=temp_root / "out",
                    workdir=temp_root,
                    files=[VideoFileItem(path=source, relative_path=Path(source.name))],
                )
            self.assertIsNone(plan.items[0].options.encoder_preset)

    def test_build_video_args_keeps_svt_without_vbv(self) -> None:
        item = _plan_item(
            "libsvtav1",
            BackendChoice.CPU,
            EncodeOptions(codec=CodecChoice.AV1, encoder_preset="5"),
        )
        item.encoder_info = _encoder_info("libsvtav1", BackendChoice.CPU, "5")
        args = build_video_args(item)
        self.assertNotIn("-maxrate", args)
        self.assertNotIn("-bufsize", args)
        self.assertIn("-preset", args)

    def test_build_video_args_uses_new_default_vbv_factors(self) -> None:
        item = _plan_item("hevc_nvenc", BackendChoice.NVENC, EncodeOptions())
        item.encoder_info = _encoder_info("hevc_nvenc", BackendChoice.NVENC, "p6")
        args = build_video_args(item)
        self.assertIn("-maxrate", args)
        self.assertIn("2500000", args)
        self.assertIn("-bufsize", args)
        self.assertIn("8000000", args)

    def test_legacy_empty_string_preset_loads_as_none(self) -> None:
        restored = preset_data_to_encode_options(
            {
                "codec": "hevc",
                "backend": "auto",
                "ratio": None,
                "min_video_kbps": 250,
                "max_video_kbps": 0,
                "container": "mp4",
                "audio_mode": "copy",
                "audio_bitrate": "128k",
                "copy_subtitles": True,
                "copy_external_subtitles": False,
                "two_pass": False,
                "preset": "",
                "pix_fmt": "yuv420p",
                "maxrate_factor": 1.25,
                "bufsize_factor": 4.0,
            }
        )
        self.assertIsNone(restored.encoder_preset)


class GuiPresetSelectionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def test_gui_shows_nvenc_and_qsv_preset_choices(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                _capabilities_by_codec(
                    [(BackendChoice.NVENC, "hevc_nvenc"), (BackendChoice.QSV, "hevc_qsv")]
                )
            )
            panel = window.options_panel
            _select_combo_data(panel.backend_combo, "nvenc")
            panel.refresh_encoder_preset_choices()
            self.assertEqual(panel.encoder_preset_combo.itemText(1), "p1")
            _select_combo_data(panel.backend_combo, "qsv")
            panel.refresh_encoder_preset_choices()
            self.assertEqual(panel.encoder_preset_combo.itemText(1), "veryfast")
        finally:
            window.close()

    def test_backend_combo_starts_with_safe_choices_until_detection_completes(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertEqual(_backend_combo_items(window), ["auto", "cpu"])
        finally:
            window.close()

    def test_encoder_detection_filters_backend_combo_to_usable_backends(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                _capabilities_by_codec(
                    [(BackendChoice.CPU, "libx265")],
                    [(BackendChoice.CPU, "libsvtav1")],
                )
            )
            self.assertEqual(_backend_combo_items(window), ["auto", "cpu"])
            panel = window.options_panel
            self.assertFalse(hasattr(panel, "parallel_qsv_check"))
        finally:
            window.close()

    def test_backend_filtering_is_codec_specific(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                _capabilities_by_codec(
                    [(BackendChoice.QSV, "hevc_qsv"), (BackendChoice.CPU, "libx265")],
                    [(BackendChoice.CPU, "libsvtav1")],
                )
            )
            panel = window.options_panel
            _select_combo_data(panel.codec_combo, "hevc")
            self.assertEqual(_backend_combo_items(window), ["auto", "qsv", "cpu"])

            _select_combo_data(panel.codec_combo, "av1")
            self.assertEqual(_backend_combo_items(window), ["auto", "cpu"])
        finally:
            window.close()

    def test_loaded_preset_refuses_to_hide_an_unavailable_backend(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                _capabilities_by_codec(
                    [(BackendChoice.CPU, "libx265")],
                    [(BackendChoice.CPU, "libsvtav1")],
                )
            )
            panel = window.options_panel
            panel.apply_options(
                EncodeOptions(
                    backend=BackendChoice.QSV,
                )
            )
            self.assertEqual(panel.backend_combo.currentData(), "qsv")
            with self.assertRaises(ValueError):
                panel.read_options()
            panel.backend_combo.setCurrentIndex(panel.backend_combo.findData("auto"))
            self.assertEqual(panel.read_options().backend, BackendChoice.AUTO)
        finally:
            window.close()

    def test_encoder_detection_failure_keeps_safe_backend_choices(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            panel = window.options_panel
            panel.backend_combo.clear()
            panel.backend_combo.addItems(["auto", "nvenc", "qsv", "cpu"])
            window._on_encoder_capability_detection_failed("boom")
            self.assertEqual(_backend_combo_items(window), ["auto", "cpu"])
        finally:
            window.close()

    def test_gui_auto_backend_uses_default_only_and_disables_combo(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            panel = window.options_panel
            _select_combo_data(panel.backend_combo, "auto")
            panel.refresh_encoder_preset_choices()
            self.assertEqual(panel.encoder_preset_combo.count(), 1)
            self.assertFalse(panel.encoder_preset_combo.isEnabled())
        finally:
            window.close()

    def test_gui_invalid_loaded_preset_falls_back_to_default(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                _capabilities_by_codec([(BackendChoice.NVENC, "hevc_nvenc")])
            )
            window.options_panel.apply_options(
                EncodeOptions(backend=BackendChoice.NVENC, encoder_preset="invalid")
            )
            self.assertIsNone(window.options_panel.encoder_preset_combo.currentData())
        finally:
            window.close()


class CliPresetValidationTestCase(unittest.TestCase):
    def test_cli_accepts_valid_preset(self) -> None:
        with (
            patch("cli.cli_entry.discover_ffmpeg_tools", return_value=(Path("ffmpeg"), Path("ffprobe"))),
            patch("cli.cli_entry.list_available_encoders", return_value={"hevc_nvenc"}),
            patch("cli.cli_entry.resolve_encoder", return_value=_encoder_info("hevc_nvenc", BackendChoice.NVENC, "p6")),
            patch("cli.cli_entry.preset_choices_for_encoder", return_value=["p5", "p6"]),
            patch("cli.cli_entry.build_encode_plan") as plan_mock,
            patch("cli.cli_entry.print_plan"),
        ):
            plan_mock.return_value = type("Plan", (), {"items": [], "warnings": []})()
            exit_code = run_cli(["plan", "input.mp4", "--backend", "nvenc", "--encoder-preset", "p6"])
        self.assertEqual(exit_code, 0)

    def test_cli_rejects_invalid_preset(self) -> None:
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            patch("cli.cli_entry.discover_ffmpeg_tools", return_value=(Path("ffmpeg"), Path("ffprobe"))),
            patch("cli.cli_entry.list_available_encoders", return_value={"hevc_nvenc"}),
            patch("cli.cli_entry.resolve_encoder", return_value=_encoder_info("hevc_nvenc", BackendChoice.NVENC, "p6")),
            patch("cli.cli_entry.preset_choices_for_encoder", return_value=["p5", "p6"]),
        ):
            exit_code = run_cli(["plan", "input.mp4", "--backend", "nvenc", "--encoder-preset", "slow"])
        self.assertEqual(exit_code, 2)
        self.assertIn("hevc_nvenc", stderr.getvalue())
        self.assertIn("p5, p6", stderr.getvalue())

    def test_cli_rejects_explicit_preset_with_auto_backend(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            exit_code = run_cli(["plan", "input.mp4", "--backend", "auto", "--encoder-preset", "slow"])
        self.assertEqual(exit_code, 2)
        self.assertIn("--encoder-preset cannot be used with --backend auto", stderr.getvalue())

    def test_cli_ignores_inherited_preset_with_auto_backend(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["plan", "input.mp4"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            options = _normalize_auto_backend_preset(EncodeOptions(encoder_preset="p6"), args)
        self.assertIsNone(options.encoder_preset)
        self.assertIn("inherited encoder preset ignored", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
