from __future__ import annotations

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QComboBox

from cli.cli_entry import _build_parser, _merge_options
from core.ffmpeg.commands import (
    build_encode_commands,
    build_input_acceleration_args,
    build_video_args,
)
from core.ffmpeg.capabilities import (
    ENCODER_CAPABILITIES_SCHEMA_VERSION,
    _valid_capability_shape,
    detect_encoder_capabilities,
    is_encoder_capability_cache_valid,
    smoke_test_encoder,
)
from core.ffmpeg.encoders import (
    iter_codec_candidates,
    default_preset_for_encoder,
    list_available_hwaccels,
    parse_hwaccels,
    preset_choices_for_encoder,
    resolve_encoder,
)
from core.models import (
    BackendChoice,
    CodecChoice,
    DecodeAcceleration,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
)
from core.encoding.planning import _validate_decode_acceleration
from core.config.store import encode_options_to_preset_data, preset_data_to_encode_options
from gui.gui_mainwindow import MainWindow


def combo_item(combo: QComboBox, index: int) -> QStandardItem:
    """Return a combo box's item, which the panel builds from a standard model."""

    model = combo.model()
    assert isinstance(model, QStandardItemModel)
    item = model.item(index)
    assert item is not None
    return item


def _preset_data(**overrides) -> dict:
    data = {
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
        "preset": None,
        "pix_fmt": "yuv420p",
        "maxrate_factor": 1.25,
        "bufsize_factor": 4.0,
    }
    data.update(overrides)
    return data


def _item(options: EncodeOptions | None = None, *, encoder: str = "hevc_videotoolbox") -> EncodePlanItem:
    current = options or EncodeOptions(
        codec=CodecChoice.HEVC,
        backend=BackendChoice.VIDEOTOOLBOX,
        overwrite=True,
    )
    return EncodePlanItem(
        source_path=Path("input.mp4"),
        output_path=Path("output.mp4"),
        media_info=None,
        encoder_info=EncoderInfo(
            codec=current.codec,
            backend=current.backend,
            encoder_name=encoder,
            supports_two_pass=False,
            default_preset=None,
        ),
        options=current,
        target_video_bitrate_bps=4_000_000,
    )


def _capabilities(hevc: list[tuple[str, str]], av1: list[tuple[str, str]] | None = None, hwaccels=None) -> dict:
    return {
        "hwaccels": list(hwaccels or []),
        "codecs": {
            "hevc": [{"backend": backend, "encoder": encoder} for backend, encoder in hevc],
            "av1": [{"backend": backend, "encoder": encoder} for backend, encoder in (av1 or [])],
        },
    }


class VideoToolboxModelAndPresetTestCase(unittest.TestCase):
    def test_enums_and_default_decode_mode(self) -> None:
        self.assertEqual(BackendChoice("videotoolbox"), BackendChoice.VIDEOTOOLBOX)
        self.assertEqual(DecodeAcceleration("software"), DecodeAcceleration.SOFTWARE)
        self.assertEqual(DecodeAcceleration("videotoolbox"), DecodeAcceleration.VIDEOTOOLBOX)
        self.assertEqual(EncodeOptions().decode_acceleration, DecodeAcceleration.SOFTWARE)

    def test_preset_round_trip_and_backward_compatibility(self) -> None:
        options = EncodeOptions(decode_acceleration=DecodeAcceleration.VIDEOTOOLBOX)
        data = encode_options_to_preset_data(options)
        self.assertEqual(data["decode_acceleration"], "videotoolbox")
        self.assertEqual(preset_data_to_encode_options(data).decode_acceleration, DecodeAcceleration.VIDEOTOOLBOX)
        self.assertEqual(preset_data_to_encode_options(_preset_data()).decode_acceleration, DecodeAcceleration.SOFTWARE)

    def test_invalid_decode_acceleration_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            preset_data_to_encode_options(_preset_data(decode_acceleration="automatic"))


class VideoToolboxEncoderMappingTestCase(unittest.TestCase):
    def test_codec_candidates_are_codec_specific(self) -> None:
        hevc = list(iter_codec_candidates(CodecChoice.HEVC))
        av1 = list(iter_codec_candidates(CodecChoice.AV1))
        self.assertIn((BackendChoice.VIDEOTOOLBOX, "hevc_videotoolbox"), hevc)
        self.assertNotIn(BackendChoice.VIDEOTOOLBOX, [backend for backend, _ in av1])
        self.assertIsNone(default_preset_for_encoder("hevc_videotoolbox"))
        with patch("core.ffmpeg.encoders._cached_runtime_preset_choices", return_value=("slow",)):
            self.assertEqual(preset_choices_for_encoder(Path("ffmpeg"), "hevc_videotoolbox"), [])

    def test_explicit_and_auto_resolution(self) -> None:
        explicit = resolve_encoder(
            CodecChoice.HEVC,
            BackendChoice.VIDEOTOOLBOX,
            {"hevc_videotoolbox"},
        )
        self.assertEqual(explicit.encoder_name, "hevc_videotoolbox")
        auto = resolve_encoder(
            CodecChoice.HEVC,
            BackendChoice.AUTO,
            set(),
            Path("ffmpeg"),
            runtime_capabilities=_capabilities(
                [("videotoolbox", "hevc_videotoolbox"), ("cpu", "libx265")],
                hwaccels=["videotoolbox"],
            ),
        )
        self.assertEqual(auto.backend, BackendChoice.VIDEOTOOLBOX)

    def test_av1_videotoolbox_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Backend videotoolbox does not support codec av1"):
            resolve_encoder(CodecChoice.AV1, BackendChoice.VIDEOTOOLBOX, set())

    def test_av1_auto_does_not_raise_key_error(self) -> None:
        encoder = resolve_encoder(CodecChoice.AV1, BackendChoice.AUTO, {"libsvtav1"})
        self.assertEqual(encoder.encoder_name, "libsvtav1")


class VideoToolboxHardwareCapabilityTestCase(unittest.TestCase):
    def test_hwaccel_parser_ignores_headings_and_normalizes(self) -> None:
        self.assertEqual(
            parse_hwaccels(
                "\n Hardware acceleration methods: \n VideoToolBox\n\n VULKAN \n"
            ),
            {"videotoolbox", "vulkan"},
        )

    def test_hwaccel_listing_uses_shared_noninteractive_policy(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffmpeg", "-hide_banner", "-hwaccels"],
            0,
            stdout="Hardware acceleration methods:\nvideotoolbox\n",
            stderr="\nvulkan\n",
        )
        with patch("core.ffmpeg.encoders.subprocess.run", return_value=completed) as run:
            self.assertEqual(list_available_hwaccels(Path("ffmpeg")), {"videotoolbox", "vulkan"})
        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_cache_shape_requires_normalized_unique_hwaccels(self) -> None:
        payload = {
            "hwaccels": ["videotoolbox"],
            "codecs": {"hevc": [], "av1": []},
        }
        self.assertTrue(_valid_capability_shape(payload))
        self.assertFalse(_valid_capability_shape({**payload, "hwaccels": ["videotoolbox", "videotoolbox"]}))
        self.assertFalse(_valid_capability_shape({**payload, "hwaccels": [""]}))
        self.assertFalse(_valid_capability_shape({"codecs": {"hevc": [], "av1": []}}))

    def test_old_schema_and_missing_hwaccels_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg = Path(temp_dir) / "ffmpeg"
            base = {
                "schema_version": ENCODER_CAPABILITIES_SCHEMA_VERSION,
                "ffmpeg_path": str(ffmpeg.resolve()),
                "ffmpeg_mtime_ns": 1,
                "ffmpeg_version": "test",
                "hwaccels": [],
                "codecs": {"hevc": [], "av1": []},
            }
            with (
                patch("core.ffmpeg.capabilities._ffmpeg_mtime_ns", return_value=1),
                patch("core.ffmpeg.capabilities._ffmpeg_version_line", return_value="test"),
            ):
                self.assertTrue(is_encoder_capability_cache_valid(base, ffmpeg))
                self.assertFalse(is_encoder_capability_cache_valid({**base, "hwaccels": None}, ffmpeg))
                self.assertFalse(
                    is_encoder_capability_cache_valid(
                        {**base, "schema_version": ENCODER_CAPABILITIES_SCHEMA_VERSION - 1}, ffmpeg
                    )
                )

    def test_video_toolbox_detection_requires_a_passing_smoke_test(self) -> None:
        with (
            patch("core.ffmpeg.capabilities._ffmpeg_mtime_ns", return_value=1),
            patch("core.ffmpeg.capabilities._ffmpeg_version_line", return_value="test"),
            patch(
                "core.ffmpeg.capabilities.smoke_test_encoder",
                side_effect=lambda _path, encoder: encoder in {"hevc_videotoolbox", "libx265", "libsvtav1"},
            ),
        ):
            capabilities = detect_encoder_capabilities(
                Path("ffmpeg"),
                available_encoders={"hevc_videotoolbox", "libx265", "libsvtav1"},
                available_hwaccels={"VULKAN", "videotoolbox"},
            )
        self.assertTrue(
            any(
                item["backend"] == "videotoolbox"
                and item["encoder"] == "hevc_videotoolbox"
                and item["preset_choices"] == []
                for item in capabilities["codecs"]["hevc"]
            )
        )
        self.assertNotIn("videotoolbox", {item["backend"] for item in capabilities["codecs"]["av1"]})
        self.assertEqual(capabilities["hwaccels"], ["videotoolbox", "vulkan"])

    def test_video_toolbox_is_omitted_when_its_smoke_test_fails(self) -> None:
        with (
            patch("core.ffmpeg.capabilities._ffmpeg_mtime_ns", return_value=1),
            patch("core.ffmpeg.capabilities._ffmpeg_version_line", return_value="test"),
            patch("core.ffmpeg.capabilities.smoke_test_encoder", return_value=False),
        ):
            capabilities = detect_encoder_capabilities(
                Path("ffmpeg"),
                available_encoders={"hevc_videotoolbox"},
                available_hwaccels={"videotoolbox"},
            )
        self.assertEqual(capabilities["codecs"]["hevc"], [])

    def test_videotoolbox_smoke_test_forces_hardware(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return type("Proc", (), {"returncode": 0})()

        with patch("core.ffmpeg.capabilities.subprocess.run", side_effect=fake_run):
            self.assertTrue(smoke_test_encoder(Path("ffmpeg"), "hevc_videotoolbox"))
        cmd = captured["cmd"]
        assert isinstance(cmd, list)
        self.assertIn("-allow_sw", cmd)
        self.assertEqual(cmd[cmd.index("-allow_sw") + 1], "0")


class VideoToolboxCommandTestCase(unittest.TestCase):
    def test_encode_arguments_are_video_toolbox_specific(self) -> None:
        item = _item()
        args = build_video_args(item)
        self.assertIn("hevc_videotoolbox", args)
        self.assertIn("-allow_sw", args)
        self.assertIn("0", args)
        self.assertIn("-tag:v", args)
        self.assertIn("hvc1", args)
        self.assertNotIn("-x265-params", args)
        self.assertNotIn("-preset", args)

    def test_decode_arguments_precede_input(self) -> None:
        options = EncodeOptions(
            decode_acceleration=DecodeAcceleration.VIDEOTOOLBOX,
            overwrite=True,
        )
        item = _item(options)
        commands, _ = build_encode_commands(Path("ffmpeg"), item, Path("workdir"))
        command = commands[0]
        self.assertEqual(command[command.index("-hwaccel") + 1], "videotoolbox")
        self.assertLess(command.index("-hwaccel"), command.index("-i"))

    def test_software_decode_adds_no_hwaccel(self) -> None:
        item = _item(EncodeOptions(decode_acceleration=DecodeAcceleration.SOFTWARE))
        self.assertEqual(build_input_acceleration_args(item), [])
        commands, _ = build_encode_commands(Path("ffmpeg"), item, Path("workdir"))
        self.assertNotIn("-hwaccel", commands[0])

    def test_planning_rejects_unavailable_explicit_decode_acceleration(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not expose the videotoolbox"):
            _validate_decode_acceleration(
                EncodeOptions(decode_acceleration=DecodeAcceleration.VIDEOTOOLBOX),
                {"hwaccels": [], "codecs": {}},
            )
        _validate_decode_acceleration(
            EncodeOptions(decode_acceleration=DecodeAcceleration.VIDEOTOOLBOX),
            {"hwaccels": ["videotoolbox"], "codecs": {}},
        )


class VideoToolboxCliAndGuiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def test_cli_parses_backend_decode_acceleration_and_override(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(
            [
                "encode",
                "input.mp4",
                "--backend",
                "videotoolbox",
                "--decode-acceleration",
                "videotoolbox",
            ]
        )
        options = _merge_options(EncodeOptions(), args)
        self.assertEqual(options.backend, BackendChoice.VIDEOTOOLBOX)
        self.assertEqual(options.decode_acceleration, DecodeAcceleration.VIDEOTOOLBOX)

        software_args = parser.parse_args(["encode", "input.mp4", "--decode-acceleration", "software"])
        self.assertEqual(
            _merge_options(EncodeOptions(decode_acceleration=DecodeAcceleration.VIDEOTOOLBOX), software_args)
            .decode_acceleration,
            DecodeAcceleration.SOFTWARE,
        )

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["encode", "input.mp4", "--decode-acceleration", "invalid"])

    def test_gui_filters_videotoolbox_by_codec_and_runtime(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            capabilities = _capabilities(
                [("videotoolbox", "hevc_videotoolbox"), ("cpu", "libx265")],
                [("cpu", "libsvtav1")],
                hwaccels=["videotoolbox"],
            )
            window._on_encoder_capability_detection_completed(capabilities)
            panel = window.options_panel
            self.assertIn("videotoolbox", [panel.backend_combo.itemText(i) for i in range(panel.backend_combo.count())])
            self.assertEqual(
                [panel.decode_acceleration_combo.itemData(i) for i in range(panel.decode_acceleration_combo.count())],
                ["software", "videotoolbox"],
            )
            self.assertTrue(combo_item(panel.decode_acceleration_combo, 1).isEnabled())

            panel.apply_options(
                EncodeOptions(
                    backend=BackendChoice.VIDEOTOOLBOX,
                    decode_acceleration=DecodeAcceleration.VIDEOTOOLBOX,
                )
            )
            self.assertEqual(panel.read_options().decode_acceleration, DecodeAcceleration.VIDEOTOOLBOX)

            panel.codec_combo.setCurrentText("av1")
            self.assertNotIn("videotoolbox", [panel.backend_combo.itemText(i) for i in range(panel.backend_combo.count())])
            self.assertEqual(panel.backend_combo.currentText(), "auto")
        finally:
            window.close()

    def test_gui_resets_unsupported_decode_selection(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            window._on_encoder_capability_detection_completed(
                _capabilities([("cpu", "libx265")], [("cpu", "libsvtav1")], hwaccels=[])
            )
            panel = window.options_panel
            panel.apply_options(EncodeOptions(decode_acceleration=DecodeAcceleration.VIDEOTOOLBOX))
            self.assertEqual(panel.decode_acceleration_combo.currentData(), "software")
            self.assertFalse(combo_item(panel.decode_acceleration_combo, 1).isEnabled())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
