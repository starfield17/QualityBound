from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import core.ffmpeg.discovery as discover_ffmpeg


def _touch_binary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


class DiscoverFfmpegTestCase(unittest.TestCase):
    def test_explicit_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            explicit = _touch_binary(temp_root / "custom_ffmpeg")
            project_root = temp_root / "project"
            _touch_binary(project_root / "FFmpeg" / "ffmpeg")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=project_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=project_root),
            ):
                resolved = discover_ffmpeg.find_binary(str(explicit), "ffmpeg")

            self.assertEqual(resolved, explicit.resolve())

    def test_missing_explicit_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing_ffmpeg"
            with self.assertRaises(FileNotFoundError):
                discover_ffmpeg.find_binary(str(missing), "ffmpeg")

    def test_project_root_binary_is_preferred_over_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            bundled = _touch_binary(project_root / "FFmpeg" / "ffmpeg")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=project_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=project_root),
                patch("core.ffmpeg.discovery.shutil.which", return_value=str(temp_root / "path_ffmpeg")),
            ):
                resolved = discover_ffmpeg.find_binary(None, "ffmpeg")

            self.assertEqual(resolved, bundled.resolve())

    def test_project_bin_layout_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            bundled = _touch_binary(project_root / "FFmpeg" / "bin" / "ffprobe")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=project_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=project_root),
                patch("core.ffmpeg.discovery.shutil.which", return_value=None),
            ):
                resolved = discover_ffmpeg.find_binary(None, "ffprobe")

            self.assertEqual(resolved, bundled.resolve())

    def test_bundle_root_is_used_as_secondary_project_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            runtime_root = temp_root / "runtime"
            bundle_root = temp_root / "bundle"
            bundled = _touch_binary(bundle_root / "FFmpeg" / "ffmpeg")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=runtime_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=bundle_root),
                patch("core.ffmpeg.discovery.shutil.which", return_value=None),
            ):
                resolved = discover_ffmpeg.find_binary(None, "ffmpeg")

            self.assertEqual(resolved, bundled.resolve())

    def test_path_fallback_is_used_when_project_binary_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            path_binary = _touch_binary(temp_root / "usr" / "bin" / "ffmpeg")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=project_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=project_root),
                patch("core.ffmpeg.discovery.shutil.which", return_value=str(path_binary)),
            ):
                resolved = discover_ffmpeg.find_binary(None, "ffmpeg")

            self.assertEqual(resolved, path_binary.resolve())

    def test_homebrew_fallback_is_checked_on_non_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            brew_prefix = temp_root / "brew"
            brew_binary = _touch_binary(brew_prefix / "bin" / "ffprobe")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=project_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=project_root),
                patch.object(discover_ffmpeg, "COMMON_HOMEBREW_PREFIXES", (brew_prefix,)),
                patch.object(discover_ffmpeg, "is_windows", return_value=False),
                patch("core.ffmpeg.discovery.shutil.which", return_value=None),
            ):
                resolved = discover_ffmpeg.find_binary(None, "ffprobe")

            self.assertEqual(resolved, brew_binary.resolve())

    def test_scoop_fallback_is_checked_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            scoop_prefix = temp_root / "scoop_ffmpeg"
            scoop_binary = _touch_binary(scoop_prefix / "bin" / "ffmpeg.exe")

            def which_side_effect(name: str) -> str | None:
                if name == "scoop":
                    return str(temp_root / "scoop.cmd")
                return None

            proc = Mock()
            proc.returncode = 0
            proc.stdout = str(scoop_prefix)

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=project_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=project_root),
                patch.object(discover_ffmpeg, "is_windows", return_value=True),
                patch("core.ffmpeg.discovery.shutil.which", side_effect=which_side_effect),
                patch("core.ffmpeg.discovery.subprocess.run", return_value=proc) as run,
            ):
                resolved = discover_ffmpeg.find_binary(None, "ffmpeg")

            self.assertEqual(resolved, scoop_binary.resolve())
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_homebrew_prefix_query_uses_noninteractive_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            brew_prefix = temp_root / "brew"
            brew_binary = _touch_binary(brew_prefix / "bin" / "ffprobe")
            brew = temp_root / "brew"
            proc = Mock(returncode=0, stdout=str(brew_prefix))

            with (
                patch.object(discover_ffmpeg, "is_windows", return_value=False),
                patch("core.ffmpeg.discovery.shutil.which", return_value=str(brew)),
                patch("core.ffmpeg.discovery.subprocess.run", return_value=proc) as run,
            ):
                resolved = discover_ffmpeg.detect_homebrew_binary("ffprobe")

            self.assertEqual(resolved, brew_binary.resolve())
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_discover_tools_can_mix_explicit_and_auto_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            project_root = temp_root / "project"
            explicit_ffmpeg = _touch_binary(temp_root / "manual_ffmpeg")
            auto_ffprobe = _touch_binary(project_root / "FFmpeg" / "ffprobe")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=project_root),
                patch.object(discover_ffmpeg, "bundle_root", return_value=project_root),
                patch("core.ffmpeg.discovery.shutil.which", return_value=None),
            ):
                ffmpeg_path, ffprobe_path = discover_ffmpeg.discover_ffmpeg_tools(str(explicit_ffmpeg), None)

            self.assertEqual(ffmpeg_path, explicit_ffmpeg.resolve())
            self.assertEqual(ffprobe_path, auto_ffprobe.resolve())

    def test_explicit_ffmpeg_prefers_its_sibling_ffprobe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            bin_dir = temp_root / "ffmpeg-build" / "bin"
            explicit_ffmpeg = _touch_binary(bin_dir / "ffmpeg")
            sibling_ffprobe = _touch_binary(bin_dir / "ffprobe")
            other_ffprobe = _touch_binary(temp_root / "other" / "ffprobe")

            with (
                patch.object(discover_ffmpeg, "app_root", return_value=temp_root / "project"),
                patch.object(discover_ffmpeg, "bundle_root", return_value=temp_root / "project"),
                patch("core.ffmpeg.discovery.shutil.which", return_value=str(other_ffprobe)),
            ):
                ffmpeg_path, ffprobe_path = discover_ffmpeg.discover_ffmpeg_tools(str(explicit_ffmpeg), None)

            self.assertEqual(ffmpeg_path, explicit_ffmpeg.resolve())
            self.assertEqual(ffprobe_path, sibling_ffprobe.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
