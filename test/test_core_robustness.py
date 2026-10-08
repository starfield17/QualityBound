from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config.store import app_config_path, load_app_config, update_app_config
from core.encoding.executor import execute_plan_item
from core.media.subtitles import discover_external_subtitles
from core.models import (
    BackendChoice,
    CodecChoice,
    CompressionMode,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
)


def _fixed_bitrate_item(root: Path, output: Path, *, overwrite: bool = True) -> EncodePlanItem:
    source = root / "source.mov"
    source.write_bytes(b"s" * 1_000)
    return EncodePlanItem(
        source_path=source,
        output_path=output,
        media_info=None,
        encoder_info=EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=False,
            default_preset="slow",
        ),
        options=EncodeOptions(
            compression_mode=CompressionMode.FIXED_BITRATE,
            overwrite=overwrite,
        ),
        target_video_bitrate_bps=1_000_000,
    )


class FixedBitratePublicationTestCase(unittest.TestCase):
    def test_successful_encode_is_published_from_a_temporary_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output.mp4"
            item = _fixed_bitrate_item(root, output)

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"x" * 500)

            with patch("core.encoding.executor._run_logged_command", side_effect=fake_run):
                result = execute_plan_item(Path("ffmpeg"), item, root)

            self.assertTrue(result.success)
            self.assertEqual(output.read_bytes(), b"x" * 500)
            self.assertFalse(list(root.glob(".*.partial-*")))

    def test_failed_encode_never_leaves_partial_output_or_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "output.mp4"
            item = _fixed_bitrate_item(root, output, overwrite=False)

            def failing_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"partial")
                raise subprocess.CalledProcessError(1, cmd, output="boom")

            with patch("core.encoding.executor._run_logged_command", side_effect=failing_run):
                result = execute_plan_item(Path("ffmpeg"), item, root)

            self.assertFalse(result.success)
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".*.partial-*")))


class AppConfigRobustnessTestCase(unittest.TestCase):
    def test_corrupt_config_falls_back_to_defaults_and_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workdir = Path(temp_dir)
            with patch("core.config.store.workdir_dir", return_value=workdir):
                config_dir = workdir / "config"
                path = app_config_path(config_dir)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{not valid json", encoding="utf-8")

                loaded = load_app_config(config_dir)

                self.assertEqual(loaded["language"], "en")
                self.assertFalse(path.exists())
                self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_update_writes_atomically_without_leaving_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workdir = Path(temp_dir)
            with patch("core.config.store.workdir_dir", return_value=workdir):
                config_dir = workdir / "config"
                update_app_config(config_dir, lambda data: {**data, "language": "zh_cn"})

                path = app_config_path(config_dir)
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["language"], "zh_cn")
                self.assertFalse(list(path.parent.glob("*.tmp")))
                self.assertFalse(list(path.parent.glob(".*.tmp")))


class ExternalSubtitleDiscoveryTestCase(unittest.TestCase):
    def test_unreadable_source_directory_is_not_fatal(self) -> None:
        with patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
            self.assertEqual(discover_external_subtitles(Path("/nonexistent/source.mkv")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
