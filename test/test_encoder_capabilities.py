from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.ffmpeg.capabilities import (
    ENCODER_CAPABILITIES_SCHEMA_VERSION,
    _ffmpeg_version_line,
    detect_encoder_capabilities,
    is_encoder_capability_cache_valid,
    smoke_test_encoder,
)
from core.ffmpeg.encoders import (
    _run_encoder_help,
    list_available_encoders,
    resolve_encoder,
)
from core.models import BackendChoice, CodecChoice


def _capabilities(hevc: list[tuple[BackendChoice, str]], av1: list[tuple[BackendChoice, str]] | None = None) -> dict:
    return {
        "hwaccels": [],
        "codecs": {
            "hevc": [{"backend": backend.value, "encoder": encoder_name} for backend, encoder_name in hevc],
            "av1": [{"backend": backend.value, "encoder": encoder_name} for backend, encoder_name in (av1 or [])],
        }
    }


def _cache_payload(ffmpeg_path: Path, **overrides) -> dict:
    payload = {
        "schema_version": ENCODER_CAPABILITIES_SCHEMA_VERSION,
        "ffmpeg_path": str(ffmpeg_path.expanduser().resolve()),
        "ffmpeg_mtime_ns": 123,
        "ffmpeg_version": "ffmpeg version test",
        "hwaccels": [],
        "codecs": {"hevc": [], "av1": []},
    }
    payload.update(overrides)
    return payload


class RuntimeCapabilityResolveTestCase(unittest.TestCase):
    def test_auto_uses_runtime_capabilities_before_encoder_listing(self) -> None:
        capabilities = _capabilities(
            [(BackendChoice.QSV, "hevc_qsv"), (BackendChoice.CPU, "libx265")],
        )
        encoder = resolve_encoder(
            CodecChoice.HEVC,
            BackendChoice.AUTO,
            {"hevc_nvenc", "hevc_qsv", "libx265"},
            Path("ffmpeg"),
            runtime_capabilities=capabilities,
        )
        self.assertEqual(encoder.backend, BackendChoice.QSV)
        self.assertEqual(encoder.encoder_name, "hevc_qsv")

    def test_runtime_capabilities_are_codec_specific(self) -> None:
        capabilities = _capabilities(
            [(BackendChoice.QSV, "hevc_qsv")],
            [(BackendChoice.CPU, "libsvtav1")],
        )
        encoder = resolve_encoder(
            CodecChoice.AV1,
            BackendChoice.AUTO,
            {"av1_qsv", "libsvtav1"},
            Path("ffmpeg"),
            runtime_capabilities=capabilities,
        )
        self.assertEqual(encoder.backend, BackendChoice.CPU)
        self.assertEqual(encoder.encoder_name, "libsvtav1")

    def test_concrete_backend_uses_runtime_smoke_test_result(self) -> None:
        capabilities = _capabilities([(BackendChoice.QSV, "hevc_qsv")])
        with self.assertRaisesRegex(RuntimeError, "not usable"):
            resolve_encoder(
                CodecChoice.HEVC,
                BackendChoice.NVENC,
                {"hevc_nvenc"},
                Path("ffmpeg"),
                runtime_capabilities=capabilities,
            )


class EncoderCapabilityCacheTestCase(unittest.TestCase):
    def test_legacy_schema_version_is_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg_path = Path(temp_dir) / "ffmpeg"

            with (
                patch("core.ffmpeg.capabilities._ffmpeg_mtime_ns", return_value=123),
                patch("core.ffmpeg.capabilities._ffmpeg_version_line", return_value="ffmpeg version test"),
            ):
                self.assertFalse(
                    is_encoder_capability_cache_valid(
                        _cache_payload(
                            ffmpeg_path,
                            schema_version=ENCODER_CAPABILITIES_SCHEMA_VERSION - 1,
                        ),
                        ffmpeg_path,
                    )
                )

    def test_cache_validation_uses_ffmpeg_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg_path = Path(temp_dir) / "ffmpeg"
            other_ffmpeg_path = Path(temp_dir) / "other-ffmpeg"

            with (
                patch("core.ffmpeg.capabilities._ffmpeg_mtime_ns", return_value=123),
                patch("core.ffmpeg.capabilities._ffmpeg_version_line", return_value="ffmpeg version test"),
            ):
                self.assertTrue(
                    is_encoder_capability_cache_valid(
                        _cache_payload(ffmpeg_path),
                        ffmpeg_path,
                    )
                )
                mismatched_payload = _cache_payload(ffmpeg_path)
                mismatched_payload["ffmpeg_path"] = str(other_ffmpeg_path.resolve())
                self.assertFalse(
                    is_encoder_capability_cache_valid(
                        mismatched_payload,
                        ffmpeg_path,
                    )
                )
                self.assertFalse(
                    is_encoder_capability_cache_valid(
                        _cache_payload(ffmpeg_path, ffmpeg_mtime_ns=456),
                        ffmpeg_path,
                    )
                )
                self.assertFalse(
                    is_encoder_capability_cache_valid(
                        _cache_payload(
                            ffmpeg_path,
                            ffmpeg_version="ffmpeg version old",
                        ),
                        ffmpeg_path,
                    )
                )

    def test_detection_filters_encoder_listing_with_smoke_tests(self) -> None:
        def fake_smoke_test(_ffmpeg_path: Path, encoder_name: str) -> bool:
            return encoder_name in {"hevc_qsv", "libx265", "libsvtav1"}

        with (
            patch("core.ffmpeg.capabilities._ffmpeg_mtime_ns", return_value=123),
            patch("core.ffmpeg.capabilities._ffmpeg_version_line", return_value="ffmpeg version test"),
            patch("core.ffmpeg.capabilities.smoke_test_encoder", side_effect=fake_smoke_test),
            patch("core.ffmpeg.capabilities.preset_choices_for_encoder", return_value=["test-preset"]),
        ):
            capabilities = detect_encoder_capabilities(
                Path("ffmpeg"),
                available_encoders={"hevc_nvenc", "hevc_qsv", "libx265", "libsvtav1"},
                available_hwaccels=set(),
            )

        self.assertEqual(
            capabilities["codecs"]["hevc"],
            [
                {"backend": "qsv", "encoder": "hevc_qsv", "preset_choices": ["test-preset"]},
                {"backend": "cpu", "encoder": "libx265", "preset_choices": ["test-preset"]},
            ],
        )
        self.assertEqual(
            capabilities["codecs"]["av1"],
            [{"backend": "cpu", "encoder": "libsvtav1", "preset_choices": ["test-preset"]}],
        )

    def test_smoke_test_uses_nvenc_compatible_frame_size(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd: list[str], **_kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = _kwargs
            return type("Proc", (), {"returncode": 0})()

        with patch("core.ffmpeg.capabilities.subprocess.run", side_effect=fake_run):
            self.assertTrue(smoke_test_encoder(Path("ffmpeg"), "hevc_nvenc"))

        command = captured["cmd"]
        run_kwargs = captured["kwargs"]
        assert isinstance(command, list) and isinstance(run_kwargs, dict)
        self.assertIn("testsrc2=size=256x256:rate=1", command)
        self.assertIs(run_kwargs["stdin"], subprocess.DEVNULL)

    def test_ffmpeg_version_query_uses_noninteractive_stdin(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffmpeg", "-version"],
            0,
            stdout="ffmpeg version test\n",
            stderr="",
        )
        with patch("core.ffmpeg.capabilities.subprocess.run", return_value=completed) as run:
            self.assertEqual(_ffmpeg_version_line(Path("ffmpeg")), "ffmpeg version test")

        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_ffmpeg_version_query_forwards_windows_creation_flag(self) -> None:
        creationflags = 0x08000000
        completed = subprocess.CompletedProcess(
            ["ffmpeg", "-version"],
            0,
            stdout="ffmpeg version test\n",
            stderr="",
        )
        with (
            patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=creationflags),
            patch("core.ffmpeg.capabilities.subprocess.run", return_value=completed) as run,
        ):
            _ffmpeg_version_line(Path("ffmpeg"))

        self.assertEqual(run.call_args.kwargs["creationflags"], creationflags)

    def test_smoke_test_forwards_windows_creation_flag(self) -> None:
        creationflags = 0x08000000
        completed = type("Proc", (), {"returncode": 0})()
        with (
            patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=creationflags),
            patch("core.ffmpeg.capabilities.subprocess.run", return_value=completed) as run,
        ):
            self.assertTrue(smoke_test_encoder(Path("ffmpeg"), "hevc_nvenc"))

        self.assertEqual(run.call_args.kwargs["creationflags"], creationflags)


class EncoderSubprocessTestCase(unittest.TestCase):
    def test_encoder_listing_uses_noninteractive_stdin(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffmpeg", "-encoders"],
            0,
            stdout=" V..... h264_nvenc       NVIDIA encoder\n",
            stderr="",
        )
        with patch("core.ffmpeg.encoders.subprocess.run", return_value=completed) as run:
            self.assertEqual(list_available_encoders(Path("ffmpeg")), {"h264_nvenc"})

        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_encoder_help_uses_noninteractive_stdin(self) -> None:
        completed = subprocess.CompletedProcess(
            ["ffmpeg", "-h", "encoder=libx265"],
            0,
            stdout="encoder help",
            stderr="",
        )
        with patch("core.ffmpeg.encoders.subprocess.run", return_value=completed) as run:
            self.assertEqual(_run_encoder_help(Path("ffmpeg"), "libx265"), "encoder help")

        self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_encoder_queries_forward_windows_creation_flag(self) -> None:
        creationflags = 0x08000000
        listing = subprocess.CompletedProcess(
            ["ffmpeg", "-encoders"],
            0,
            stdout=" V..... h264_nvenc       NVIDIA encoder\n",
            stderr="",
        )
        help_output = subprocess.CompletedProcess(
            ["ffmpeg", "-h", "encoder=libx265"],
            0,
            stdout="encoder help",
            stderr="",
        )
        with (
            patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=creationflags),
            patch("core.ffmpeg.encoders.subprocess.run", side_effect=[listing, help_output]) as run,
        ):
            list_available_encoders(Path("ffmpeg"))
            _run_encoder_help(Path("ffmpeg"), "libx265")

        for call in run.call_args_list:
            self.assertEqual(call.kwargs["creationflags"], creationflags)


if __name__ == "__main__":
    unittest.main(verbosity=2)
