from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config.store import app_config_path, load_app_config, update_app_config
from core.encoding.executor import execute_plan_item
from core.ffmpeg.commands import build_encode_commands
from core.ffmpeg.probe import _run_command
from core.media.discovery import collect_video_files
from core.media.subtitles import discover_external_subtitles
from core.progress_events import ProgressEvent
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
    OperationCancelledError,
)
from gui.queue_state import (
    QueueItemRecord,
    QueueItemStatus,
    QueueJobSnapshot,
    compute_metrics,
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
    # N3 ← S2
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


class SkippedOutputCollisionTestCase(unittest.TestCase):
    def _source(self, directory: Path, name: str) -> Path:
        source = directory / name
        source.write_bytes(b"video")
        return source

    def _capabilities(self) -> dict[str, object]:
        return {
            "hwaccels": [],
            "codecs": {
                "hevc": [
                    {
                        "backend": BackendChoice.CPU.value,
                        "encoder": "libx265",
                        "preset_choices": ["slow"],
                    }
                ],
                "av1": [],
            },
        }

    def _media(self, path: Path) -> MediaInfo:
        return MediaInfo(
            path=path,
            duration=10.0,
            format_bitrate_bps=2_000_000,
            video_bitrate_bps=1_800_000,
            audio_bitrate_bps=128_000,
            width=1280,
            height=720,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
        )

    def _build(self, folder: Path, probe: object) -> EncodePlan:
        from core.encoding import build_encode_plan

        with (
            patch(
                "core.encoding.planning.discover_ffmpeg_tools",
                return_value=(folder / "ffmpeg", folder / "ffprobe"),
            ),
            patch(
                "core.encoding.planning.ensure_encoder_capabilities",
                return_value=self._capabilities(),
            ),
            patch("core.encoding.planning.probe_media_info", side_effect=probe),
        ):
            return build_encode_plan(
                input_path=folder,
                options=EncodeOptions(
                    backend=BackendChoice.CPU,
                    encoder_preset="slow",
                    overwrite=True,
                    copy_external_subtitles=False,
                ),
                output_dir=None,
                workdir=folder / "work",
            )

    def test_probe_failure_is_skipped_without_aborting_on_shared_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            self._source(folder, "clip.mkv")
            self._source(folder, "clip.mp4")

            def probe(_ffprobe: Path, source: Path, **_kwargs: object) -> MediaInfo:
                if source.suffix == ".mp4":
                    raise RuntimeError("cannot probe")
                return self._media(source)

            plan = self._build(folder, probe)

            self.assertEqual(len(plan.items), 2)
            by_name = {item.source_path.name: item for item in plan.items}
            self.assertIsNone(by_name["clip.mkv"].skip_reason)
            self.assertEqual(by_name["clip.mp4"].skip_reason, "cannot probe")
            self.assertEqual(by_name["clip.mkv"].output_path, by_name["clip.mp4"].output_path)

    def test_two_live_items_sharing_an_output_still_abort_the_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            self._source(folder, "clip.mkv")
            self._source(folder, "clip.mp4")

            def probe(_ffprobe: Path, source: Path, **_kwargs: object) -> MediaInfo:
                return self._media(source)

            with self.assertRaisesRegex(RuntimeError, "collision"):
                self._build(folder, probe)


class SavedBytesEstimateTestCase(unittest.TestCase):
    def _record(
        self,
        folder: Path,
        name: str,
        *,
        mode: CompressionMode,
        status: QueueItemStatus,
        target_bps: int,
        size_bytes: int,
    ) -> QueueItemRecord:
        source = folder / f"{name}.mkv"
        source.write_bytes(b"x" * size_bytes)
        media = MediaInfo(
            path=source,
            duration=10.0,
            format_bitrate_bps=8_000_000,
            video_bitrate_bps=7_000_000,
            audio_bitrate_bps=128_000,
            width=1920,
            height=1080,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
        )
        item = EncodePlanItem(
            source_path=source,
            output_path=folder / f"{name}.mp4",
            media_info=media,
            encoder_info=None,
            options=EncodeOptions(compression_mode=mode, max_output_ratio=0.5),
            target_video_bitrate_bps=target_bps,
        )
        return QueueItemRecord(
            item_id=name,
            plan_item=item,
            job_snapshot=QueueJobSnapshot(
                workdir=folder,
                ffmpeg_path=Path("ffmpeg"),
                ffprobe_path=Path("ffprobe"),
                output_root=folder,
            ),
            status=status,
            total_passes=1,
        )

    def test_smart_item_without_analysis_reports_a_floor_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            record = self._record(
                folder,
                "smart",
                mode=CompressionMode.SMART,
                status=QueueItemStatus.WAITING_ANALYSIS,
                target_bps=0,
                size_bytes=1_000_000,
            )

            metrics = compute_metrics([record])

            self.assertEqual(metrics.estimated_saved_bytes, 500_000)
            self.assertTrue(metrics.estimated_saved_is_floor)

    def test_fixed_bitrate_estimate_is_not_flagged_as_a_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            record = self._record(
                folder,
                "fixed",
                mode=CompressionMode.FIXED_BITRATE,
                status=QueueItemStatus.QUEUED,
                target_bps=800_000,
                size_bytes=20_000_000,
            )

            metrics = compute_metrics([record])

            self.assertIsNotNone(metrics.estimated_saved_bytes)
            self.assertFalse(metrics.estimated_saved_is_floor)

    def test_skipped_smart_item_does_not_contribute_a_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            record = self._record(
                folder,
                "skipped",
                mode=CompressionMode.SMART,
                status=QueueItemStatus.SKIPPED,
                target_bps=0,
                size_bytes=1_000_000,
            )

            metrics = compute_metrics([record])

            self.assertIsNone(metrics.estimated_saved_bytes)
            self.assertFalse(metrics.estimated_saved_is_floor)


class AppConfigRobustnessTestCase(unittest.TestCase):
    def test_corrupt_config_falls_back_to_defaults_and_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workdir = Path(temp_dir)
            with patch("core.config.store.workdir_dir", return_value=workdir):
                path = app_config_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{not valid json", encoding="utf-8")

                loaded = load_app_config()

                self.assertEqual(loaded["language"], "en")
                self.assertFalse(path.exists())
                self.assertTrue(path.with_name(path.name + ".corrupt").exists())

    def test_update_writes_atomically_without_leaving_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workdir = Path(temp_dir)
            with patch("core.config.store.workdir_dir", return_value=workdir):
                update_app_config(lambda data: {**data, "language": "zh_cn"})

                path = app_config_path()
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["language"], "zh_cn")
                self.assertFalse(list(path.parent.glob("*.tmp")))
                self.assertFalse(list(path.parent.glob(".*.tmp")))


class ExternalSubtitleDiscoveryTestCase(unittest.TestCase):
    def test_unreadable_source_directory_is_not_fatal(self) -> None:
        with patch.object(Path, "iterdir", side_effect=OSError("permission denied")):
            self.assertEqual(discover_external_subtitles(Path("/nonexistent/source.mkv")), [])


class ProbeCommandLifecycleTestCase(unittest.TestCase):
    def _slow_command(self) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    def test_command_output_is_captured(self) -> None:
        result = _run_command([sys.executable, "-c", "print('hello')"])
        self.assertEqual(result.stdout.strip(), "hello")

    def test_command_timeout_is_reported(self) -> None:
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            _run_command(self._slow_command(), timeout_sec=0.3)
        self.assertLess(time.monotonic() - start, 20.0)

    def test_command_cancel_terminates_the_process(self) -> None:
        with self.assertRaises(OperationCancelledError):
            _run_command(self._slow_command(), timeout_sec=30.0, cancel_check=lambda: True)

    def test_nonzero_exit_reports_stderr(self) -> None:
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            _run_command(
                [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]
            )
        self.assertEqual(caught.exception.returncode, 3)
        self.assertIn("boom", caught.exception.stderr or "")


class VideoDiscoveryErrorReportingTestCase(unittest.TestCase):
    def test_unreadable_subfolder_is_reported_and_batch_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "ok.mp4").write_bytes(b"x")
            locked = root / "locked"
            locked.mkdir()
            (locked / "hidden.mp4").write_bytes(b"x")
            locked_resolved = locked.resolve()
            reported: list[str] = []
            real_scandir = os.scandir

            def fake_scandir(path: object) -> object:
                if Path(str(path)) == locked_resolved:
                    raise PermissionError(13, "Permission denied", str(path))
                return real_scandir(str(path))

            with patch("core.media.discovery.os.scandir", side_effect=fake_scandir):
                files = collect_video_files(root, recursive=True, on_error=reported.append)

            self.assertEqual([item.path.name for item in files], ["ok.mp4"])
            self.assertTrue(any("unreadable folder" in message.lower() for message in reported))

    def test_unreadable_root_folder_raises_a_clear_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with patch(
                "core.media.discovery.os.scandir",
                side_effect=PermissionError(13, "Permission denied", str(root)),
            ):
                with self.assertRaisesRegex(RuntimeError, "Cannot read folder"):
                    collect_video_files(root, recursive=False)


class SystemPowerCommandTestCase(unittest.TestCase):
    def test_power_command_does_not_attach_stdin(self) -> None:
        completed = subprocess.CompletedProcess(["cmd"], 0, stdout="", stderr="")
        with patch("core.media.system_power.subprocess.run", return_value=completed) as run:
            from core.media.system_power import _run_single_command

            _run_single_command(["cmd"])
        self.assertEqual(run.call_args.kwargs.get("stdin"), subprocess.DEVNULL)


class ProcessPipeCleanupTestCase(unittest.TestCase):
    def test_cancelled_run_closes_child_pipes(self) -> None:
        from core.encoding.process import _run_logged_command

        captured: list[object] = []
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "log.txt"
            with self.assertRaises(OperationCancelledError):
                _run_logged_command(
                    [sys.executable, "-c", "print('line', flush=True); import time; time.sleep(30)"],
                    log_path,
                    cancel_check=lambda: True,
                    process_callback=captured.append,
                )
        proc = captured[0]
        assert isinstance(proc, subprocess.Popen)
        assert proc.stdout is not None
        assert proc.stdin is not None
        self.assertTrue(proc.stdout.closed)
        self.assertTrue(proc.stdin.closed)


class TwoPassCommandTestCase(unittest.TestCase):
    def test_null_muxer_pass_omits_the_hvc1_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"v")
            item = EncodePlanItem(
                source_path=source,
                output_path=root / "out.mp4",
                media_info=None,
                encoder_info=EncoderInfo(
                    codec=CodecChoice.HEVC,
                    backend=BackendChoice.CPU,
                    encoder_name="libx265",
                    supports_two_pass=True,
                    default_preset="slow",
                ),
                options=EncodeOptions(two_pass=True, overwrite=True),
                target_video_bitrate_bps=1_000_000,
            )

            commands, _ = build_encode_commands(Path("ffmpeg"), item, root)

            self.assertEqual(len(commands), 2)
            self.assertNotIn("-tag:v", commands[0])
            self.assertIn("-tag:v", commands[1])
            self.assertIn("null", commands[0])


class EncodePhaseEventTestCase(unittest.TestCase):
    def _plan(self, folder: Path) -> EncodePlan:
        source = folder / "clip.mkv"
        source.write_bytes(b"v")
        item = EncodePlanItem(
            source_path=source,
            output_path=folder / "clip.mp4",
            media_info=None,
            encoder_info=None,
            options=EncodeOptions(),
            skip_reason="planned skip",
        )
        return EncodePlan(
            items=[item],
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            input_root=folder,
            output_root=folder,
        )

    def test_serial_pause_after_analysis_emits_a_paused_event(self) -> None:
        from core.encoding.executor import execute_plan

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            events: list[ProgressEvent] = []
            with patch("core.encoding.executor.run_analysis_phase", return_value=[None]):
                results = execute_plan(
                    self._plan(folder),
                    folder,
                    progress_callback=events.append,
                    pause_check=lambda: True,
                )
            self.assertEqual(results, [])
            self.assertTrue(any(event.get("state") == "paused" for event in events))

    def test_concurrent_with_no_pending_items_omits_zero_worker_start(self) -> None:
        from core.encoding.parallel import execute_plan_concurrent

        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            plan = self._plan(folder)
            item = plan.items[0]
            finished = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=True,
            )
            logs: list[str] = []
            events: list[ProgressEvent] = []
            with patch("core.encoding.parallel.run_analysis_phase", return_value=[finished]):
                results = execute_plan_concurrent(
                    plan,
                    folder,
                    max_workers=2,
                    log_callback=logs.append,
                    progress_callback=events.append,
                )
            self.assertEqual(len(results), 1)
            self.assertFalse(any(event.get("state") == "started" for event in events))
            self.assertTrue(any("no pending items" in message for message in logs))
            self.assertTrue(any(event.get("state") == "finished" for event in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
