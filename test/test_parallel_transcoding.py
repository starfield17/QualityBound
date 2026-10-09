from __future__ import annotations

import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cli.cli_entry import _build_parser, run_cli
from core.config.store import (
    _default_app_config,
    encode_options_to_preset_data,
    parse_encode_workers,
    preset_data_to_encode_options,
)
from core.encoding import execute_plan_concurrent
from core.i18n import get_translator
from core.media.validation import validate_workdir
from core.models import (
    BackendChoice,
    CodecChoice,
    CompressionMode,
    EncodeOptions,
    EncodePlan,
    EncodePlanItem,
    EncodeResult,
    EncoderInfo,
    MediaInfo,
)
from gui.gui_mainwindow import MainWindow
from gui.settings_dialog import SettingsDialog


def _media(path: Path) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=10.0,
        format_bitrate_bps=4_000_000,
        video_bitrate_bps=3_000_000,
        audio_bitrate_bps=128_000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
    )


def _encoder(codec: CodecChoice, backend: BackendChoice) -> EncoderInfo:
    names = {
        (CodecChoice.HEVC, BackendChoice.CPU): "libx265",
        (CodecChoice.HEVC, BackendChoice.NVENC): "hevc_nvenc",
        (CodecChoice.AV1, BackendChoice.CPU): "libsvtav1",
        (CodecChoice.AV1, BackendChoice.NVENC): "av1_nvenc",
    }
    return EncoderInfo(
        codec=codec,
        backend=backend,
        encoder_name=names[(codec, backend)],
        supports_two_pass=backend == BackendChoice.CPU,
        default_preset="slow" if backend == BackendChoice.CPU else "p6",
    )


def _plan(root: Path, count: int = 4) -> EncodePlan:
    items: list[EncodePlanItem] = []
    for index in range(count):
        codec = CodecChoice.HEVC if index % 2 == 0 else CodecChoice.AV1
        backend = BackendChoice.CPU if index % 3 == 0 else BackendChoice.NVENC
        source = root / f"video_{index}.mp4"
        options = EncodeOptions(
            codec=codec,
            backend=backend,
            compression_mode=CompressionMode.FIXED_BITRATE,
            overwrite=True,
            two_pass=backend == BackendChoice.CPU,
            encoder_preset="slow" if backend == BackendChoice.CPU else "p6",
        )
        items.append(
            EncodePlanItem(
                source_path=source,
                output_path=root / f"video_{index}.mkv",
                media_info=_media(source),
                encoder_info=_encoder(codec, backend),
                options=options,
                target_video_bitrate_bps=2_000_000,
            )
        )
    return EncodePlan(
        items=items,
        ffmpeg_path=root / "ffmpeg",
        ffprobe_path=root / "ffprobe",
        input_root=root,
        output_root=root / "out",
    )


class ConcurrentConfigTestCase(unittest.TestCase):
    def test_encode_workers_defaults_and_validation(self) -> None:
        defaults = _default_app_config()
        self.assertEqual(defaults["encode_workers"], 1)
        self.assertEqual(defaults["post_encode_action"], "none")
        self.assertTrue(defaults["desktop_notifications"])
        self.assertEqual(parse_encode_workers(1), 1)
        self.assertEqual(parse_encode_workers("8"), 8)
        for invalid in (None, "no", 0, 9, -1, True):
            with self.subTest(invalid=invalid):
                self.assertEqual(parse_encode_workers(invalid), 1)

    def test_old_parallel_preset_fields_are_ignored_and_not_written(self) -> None:
        options = EncodeOptions()
        data = encode_options_to_preset_data(options)
        self.assertNotIn("parallel_enabled", data)
        self.assertNotIn("parallel_backends", data)
        data["parallel_enabled"] = True
        data["parallel_backends"] = ["nvenc", "qsv"]
        restored = preset_data_to_encode_options(data)
        self.assertFalse(hasattr(restored, "parallel_enabled"))
        self.assertFalse(hasattr(restored, "parallel_backends"))

    def test_cli_jobs_is_encode_only_and_bounded(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["encode", "input.mp4", "--jobs", "8"])
        self.assertEqual(args.jobs, 8)
        with self.assertRaises(SystemExit):
            parser.parse_args(["plan", "input.mp4", "--jobs", "2"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["encode", "input.mp4", "--jobs", "9"])

    def test_removed_cli_parallel_flags_are_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit):
            _build_parser().parse_args(["encode", "input.mp4", "--parallel"])
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_preview_command_is_removed(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _build_parser().parse_args(["preview", "input.mp4"])

    def test_workdir_does_not_create_or_delete_preview_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            validate_workdir(root)
            self.assertFalse((root / "preview").exists())
            preview = root / "preview"
            preview.mkdir()
            sample = preview / "user-sample.mp4"
            sample.write_bytes(b"keep")
            validate_workdir(root)
            self.assertEqual(sample.read_bytes(), b"keep")


class ConcurrentSchedulerTestCase(unittest.TestCase):
    # F4 ← S2
    def test_workers_preserve_bound_encoders_limit_concurrency_and_result_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _plan(root, count=6)
            original_bindings = [item.encoder_info for item in plan.items]
            active = 0
            max_active = 0
            observed: list[tuple[str, CodecChoice, BackendChoice, bool, str | None]] = []
            lock = threading.Lock()

            def fake_execute(_ffmpeg, item, _workdir, **_kwargs):
                nonlocal active, max_active
                assert item.encoder_info is not None
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.02)
                with lock:
                    observed.append(
                        (
                            item.source_path.name,
                            item.encoder_info.codec,
                            item.encoder_info.backend,
                            item.options.two_pass,
                            item.options.encoder_preset,
                        )
                    )
                    active -= 1
                return EncodeResult(item.source_path, item.output_path, success=True)

            with patch("core.encoding.parallel.execute_plan_item", side_effect=fake_execute):
                results = execute_plan_concurrent(plan, root, max_workers=3)

            self.assertEqual(max_active, 3)
            self.assertEqual(
                [result.source_path.name for result in results],
                [item.source_path.name for item in plan.items],
            )
            self.assertEqual([item.encoder_info for item in plan.items], original_bindings)
            expected = {
                (
                    item.source_path.name,
                    item.encoder_info.codec,
                    item.encoder_info.backend,
                    item.options.two_pass,
                    item.options.encoder_preset,
                )
                for item in plan.items
                if item.encoder_info is not None
            }
            self.assertEqual(set(observed), expected)

    def test_failed_result_does_not_stop_other_items(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _plan(root, count=5)

            def fake_execute(_ffmpeg, item, _workdir, **_kwargs):
                failed = item.source_path.name == "video_1.mp4"
                return EncodeResult(
                    item.source_path,
                    item.output_path,
                    success=not failed,
                    error_message="expected failure" if failed else None,
                )

            with patch("core.encoding.parallel.execute_plan_item", side_effect=fake_execute):
                results = execute_plan_concurrent(plan, root, max_workers=2)

            self.assertEqual(len(results), 5)
            self.assertEqual(sum(result.success for result in results), 4)

    def test_unexpected_worker_exception_stops_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _plan(root, count=8)

            def fake_execute(_ffmpeg, item, _workdir, **_kwargs):
                if item.source_path.name == "video_1.mp4":
                    raise RuntimeError("boom")
                time.sleep(0.01)
                return EncodeResult(item.source_path, item.output_path, success=True)

            with (
                patch("core.encoding.parallel.execute_plan_item", side_effect=fake_execute),
                self.assertRaisesRegex(RuntimeError, "boom"),
            ):
                execute_plan_concurrent(plan, root, max_workers=2)

    def test_worker_count_and_bound_encoder_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _plan(root, count=1)
            for invalid in (0, 9, True):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "1 to 8"):
                    execute_plan_concurrent(plan, root, max_workers=invalid)
            plan.items[0].encoder_info = None
            with self.assertRaisesRegex(ValueError, "bound encoder"):
                execute_plan_concurrent(plan, root, max_workers=1)

    def test_pause_waits_for_active_encodes_and_claims_no_more(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _plan(root, count=6)
            pause = threading.Event()
            both_active = threading.Barrier(2)
            started: list[str] = []
            lock = threading.Lock()

            def fake_execute(_ffmpeg, item, _workdir, **_kwargs):
                with lock:
                    started.append(item.source_path.name)
                    if len(started) == 2:
                        pause.set()
                both_active.wait(timeout=1)
                time.sleep(0.01)
                return EncodeResult(item.source_path, item.output_path, success=True)

            with patch("core.encoding.parallel.execute_plan_item", side_effect=fake_execute):
                results = execute_plan_concurrent(
                    plan,
                    root,
                    max_workers=2,
                    pause_check=pause.is_set,
                )

            self.assertEqual(len(started), 2)
            self.assertEqual(len(results), 2)


class ConcurrentCliAndGuiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def test_cli_jobs_dispatches_to_concurrent_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _plan(root, count=1)
            result = EncodeResult(plan.items[0].source_path, plan.items[0].output_path, success=True)
            with (
                patch("cli.cli_entry.build_encode_plan", return_value=plan),
                patch("cli.cli_entry.execute_plan_concurrent", return_value=[result]) as concurrent,
                patch("cli.cli_entry.print_plan"),
                patch("cli.cli_entry.print_encode_results"),
            ):
                exit_code = run_cli(["encode", "input.mp4", "--jobs", "3"])
            self.assertEqual(exit_code, 0)
            self.assertEqual(concurrent.call_args.kwargs["max_workers"], 3)

    def test_cli_default_uses_one_serial_encode_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = _plan(root, count=1)
            result = EncodeResult(plan.items[0].source_path, plan.items[0].output_path, success=True)
            with (
                patch("cli.cli_entry.build_encode_plan", return_value=plan),
                patch("cli.cli_entry.execute_plan", return_value=[result]) as serial,
                patch("cli.cli_entry.execute_plan_concurrent") as concurrent,
                patch("cli.cli_entry.print_plan"),
                patch("cli.cli_entry.print_encode_results"),
            ):
                exit_code = run_cli(["encode", "input.mp4"])
            self.assertEqual(exit_code, 0)
            serial.assert_called_once()
            concurrent.assert_not_called()

    def test_settings_owns_concurrency_and_encode_panel_has_no_parallel_controls(self) -> None:
        dialog = SettingsDialog(
            get_translator("en", self.repo_root / "config"),
            {"language": "en", "encode_workers": 6},
        )
        try:
            self.assertEqual(dialog.encode_workers_spin.value(), 6)
            self.assertEqual(dialog.values()["encode_workers"], 6)
        finally:
            dialog.close()

        default_dialog = SettingsDialog(
            get_translator("en", self.repo_root / "config"),
            {"language": "en"},
        )
        try:
            self.assertEqual(default_dialog.values()["encode_workers"], 1)
        finally:
            default_dialog.close()

        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertFalse(hasattr(window, "preview_action"))
            self.assertFalse(hasattr(window.options_panel, "parallel_check"))
            self.assertFalse(hasattr(window.options_panel, "preview_tab"))
            self.assertFalse(hasattr(window.options_panel, "advanced_tab"))
            self.assertTrue(window.options_panel.backend_combo.isEnabled())
            window.app_config.pop("encode_workers", None)
            with patch.object(window.queue_manager, "start", return_value=True) as start:
                window._start_queue()
            start.assert_called_once_with(max_workers=1)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
