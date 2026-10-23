from __future__ import annotations

import json
import io
from collections.abc import Callable
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QCoreApplication, QPoint, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from core.i18n import Translator
from gui.comparison.decode import (
    ComparisonMedia, DecodeCancelled, DecodedFrame, FramePair, ToolRunner,
    frame_time, inspect_pair, inspect_video,
)
from gui.comparison.state import (
    ComparisonPhase, ComparisonTimeline, VideoDetails, select_frame,
)
from gui.comparison.window import ComparisonWindow
from gui.comparison.playback import (AudioPlaybackWorker, PlaybackClock, PlaybackSession,
                                     PcmBuffer, VideoPlaybackWorker, audio_output_device, read_ppm)


ROOT = Path(__file__).resolve().parent.parent


class TimelineTests(unittest.TestCase):
    def test_offsets_define_common_interval_in_a_coordinates(self) -> None:
        positive = ComparisonTimeline(10, 12, 3)
        self.assertEqual((positive.start, positive.end), (0, 9))
        self.assertEqual(positive.positions(100), (9, 12))
        negative = ComparisonTimeline(10, 12, -3)
        self.assertEqual((negative.start, negative.end), (3, 10))
        self.assertEqual(negative.positions(0), (3, 0))
        self.assertFalse(ComparisonTimeline(10, 12, 12).available)
        self.assertFalse(ComparisonTimeline(10, 12, -10).available)
        delayed = ComparisonTimeline(6, 12, 0, start_a=2)
        self.assertEqual((delayed.start, delayed.end), (2, 6))

    def test_vfr_frames_use_timestamps_and_neighbours(self) -> None:
        times = [0.0, 0.04, 0.11, 0.13, 0.3]
        self.assertEqual(select_frame(times, 0.12), 0.11)
        self.assertEqual(select_frame(times, 0.12, 1), 0.13)
        self.assertEqual(select_frame(times, 0.12, -1), 0.04)
        self.assertEqual(select_frame(times, 0, -1), 0)
        self.assertEqual(select_frame(times, 99, 1), 0.3)
        with self.assertRaises(ValueError):
            select_frame([], 0)

    def test_invalid_video_timing_is_rejected(self) -> None:
        for duration, fps in [(0, 30), (10, 0), (float("nan"), 30)]:
            with self.assertRaises(ValueError):
                VideoDetails(duration, 0, fps)


class DecodeTests(unittest.TestCase):
    def test_ppm_pipe_reads_one_frame_without_eating_the_next(self) -> None:
        frame = b"P6\n1 1\n255\n\xff\x00\x00"
        stream = io.BytesIO(frame * 2)
        first = read_ppm(stream)
        second = read_ppm(stream)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.pixelColor(0, 0).red(), 255)
        self.assertEqual(first, second)
        self.assertIsNone(read_ppm(stream))
        with self.assertRaises(ValueError):
            read_ppm(io.BytesIO(frame[:-1]))

    def test_shared_playback_clock_starts_after_preparation(self) -> None:
        clock = PlaybackClock(2)
        self.assertEqual(clock.now(), 2)
        with patch("gui.comparison.playback.time.monotonic", side_effect=[10, 12]):
            clock.start()
            self.assertEqual(clock.now(), 4)

    def test_audio_progress_clock_does_not_advance_during_underrun(self) -> None:
        clock = PlaybackClock(2)
        clock.audio_master = True
        clock.start()
        clock.update_audio(0.3)
        with patch("gui.comparison.playback.time.monotonic", return_value=100000):
            self.assertEqual(clock.now(), 2.3)
        clock.update_audio(0.3)
        self.assertEqual(clock.now(), 2.3)
        clock.update_audio(0.32)
        self.assertEqual(clock.now(), 2.32)

    def test_late_video_frames_are_skipped_before_presenting(self) -> None:
        app = QApplication.instance() or QApplication([])
        clock = MagicMock(spec=PlaybackClock)
        clock.wait_until.return_value = True
        clock.now.return_value = 0
        worker = VideoPlaybackWorker(Path("explicit-ffmpeg"), Path("a.mp4"), 0, 0, clock, app)
        timestamps = [0, .04, .08, .12, .16, .20, .24, .28, .32]
        for stamp in timestamps:
            worker.timestamps.put(stamp)
        shown: list[float] = []

        def presented(_image: object, stamp: float) -> None:
            shown.append(stamp)
            clock.now.return_value = .3
            worker.presented.set()

        worker.frame_ready.connect(presented)
        ppm = b"P6\n1 1\n255\n\xff\x00\x00"
        worker._consume(io.BytesIO(ppm * len(timestamps)))
        self.assertEqual(shown, [0, .24, .28, .32])

    def test_audio_initialization_restores_qt_plugin_paths(self) -> None:
        app = QApplication.instance() or QApplication([])
        original = QCoreApplication.libraryPaths()

        def selected_device() -> object:
            self.assertEqual(QCoreApplication.libraryPaths(), [])
            raise ValueError("device unavailable")

        with patch("gui.comparison.playback.QMediaDevices.defaultAudioOutput", side_effect=selected_device):
            with self.assertRaisesRegex(ValueError, "device unavailable"):
                audio_output_device()
        self.assertEqual(QCoreApplication.libraryPaths(), original)
        self.assertIsNotNone(app)

    def test_probe_uses_video_and_container_metadata(self) -> None:
        runner = MagicMock(spec=ToolRunner)
        runner.run.return_value = json.dumps({
            "streams": [{"avg_frame_rate": "30000/1001", "duration": "5.5"}],
            "format": {"duration": "5.5", "start_time": "2.0"},
        }).encode()
        result = inspect_video(Path("video.mkv"), Path("ffprobe"), runner)
        self.assertEqual(result.duration, 5.5)
        self.assertEqual(result.origin, 2)
        self.assertAlmostEqual(result.fps, 30000 / 1001)

    def test_shifted_container_duration_is_replaced_by_actual_video_end(self) -> None:
        runner = MagicMock(spec=ToolRunner)
        runner.run.side_effect = [json.dumps({
            "streams": [{"avg_frame_rate": "25/1"}],
            "format": {"duration": "6", "start_time": "2"},
        }).encode(), json.dumps({"frames": [
            {"best_effort_timestamp_time": "5.96", "duration_time": "0.04"},
        ]}).encode()]
        result = inspect_video(Path("shifted.mkv"), Path("ffprobe"), runner)
        self.assertEqual(result.duration, 4)
        self.assertEqual(result.origin, 2)

    def test_delayed_video_span_uses_container_relative_endpoint(self) -> None:
        runner = MagicMock(spec=ToolRunner)
        runner.run.return_value = json.dumps({
            "streams": [{"avg_frame_rate": "25/1", "start_time": "2", "duration": "4"}],
            "format": {"duration": "6", "start_time": "0"},
        }).encode()
        result = inspect_video(Path("delayed.mp4"), Path("ffprobe"), runner)
        self.assertEqual(result.duration, 6)
        self.assertEqual(result.start, 2)

    def test_probe_rejects_audio_only(self) -> None:
        runner = MagicMock(spec=ToolRunner)
        runner.run.return_value = b'{"streams": []}'
        with self.assertRaisesRegex(ValueError, "No video stream"):
            inspect_video(Path("audio.m4a"), Path("ffprobe"), runner)

    def test_frame_probe_has_absolute_end_and_normalizes_origin(self) -> None:
        runner = MagicMock(spec=ToolRunner)
        runner.run.return_value = json.dumps({"frames": [
            {"best_effort_timestamp_time": str(value)} for value in (2, 2.04, 2.11, 2.13)
        ]}).encode()
        media = ComparisonMedia((Path("a"), Path("b")),
                                (VideoDetails(10, 2, 25), VideoDetails(10, 0, 30)),
                                Path("ffmpeg"), Path("ffprobe"))
        self.assertAlmostEqual(frame_time(media, 0, 0.12, 0, runner), 0.11)
        command = runner.run.call_args.args[0]
        interval = command[command.index("-read_intervals") + 1]
        self.assertEqual(interval, "2.000000%4.120000")

    def test_explicit_invalid_tool_is_not_replaced(self) -> None:
        with self.assertRaises(FileNotFoundError):
            inspect_pair((Path(__file__), Path(__file__)),
                         "/missing/comparison-ffmpeg", None, ToolRunner())

    def test_sparse_vfr_probe_expands_until_next_frame_or_eof(self) -> None:
        runner = MagicMock(spec=ToolRunner)
        runner.run.side_effect = [json.dumps({"frames": [
            {"best_effort_timestamp_time": "1.92"},
        ]}).encode(), json.dumps({"frames": [
            {"best_effort_timestamp_time": "1.92"},
            {"best_effort_timestamp_time": "1.96"},
            {"best_effort_timestamp_time": "12.0"},
        ]}).encode()]
        media = ComparisonMedia((Path("a"), Path("b")),
                                (VideoDetails(14, 0, 25), VideoDetails(14, 0, 25)),
                                Path("ffmpeg"), Path("ffprobe"))
        self.assertEqual(frame_time(media, 0, 1.96, 1, runner), 12)
        self.assertEqual(runner.run.call_count, 2)

    def test_cancellation_stops_running_child(self) -> None:
        runner = ToolRunner()
        timer = threading.Timer(0.15, runner.cancel)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaises(DecodeCancelled):
                runner.run([sys.executable, "-c", "import time; time.sleep(30)"])
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 3)


class ComparisonWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.translator = Translator('en')

    def setUp(self) -> None:
        self.window = ComparisonWindow(self.translator)
        self.window.precision.setCurrentIndex(1)
        self.window.media = ComparisonMedia(
            (Path("a.mp4"), Path("b.mp4")),
            (VideoDetails(10, 0, 25), VideoDetails(12, 0, 30)),
            Path("ffmpeg"), Path("ffprobe"),
        )
        self.window.paths = list(self.window.media.paths)
        self.session = MagicMock(spec=PlaybackSession)
        self.session.audio_available = True
        self.player_patch = patch.object(self.window, "playback", self.session)
        self.player_patch.start()
        self.png = b"P6\n4 4\n255\n" + b"\x00\x00\xff" * 16
        self.preview_patch = patch("gui.comparison.window.preview_pair", side_effect=self._preview)
        self.preview_patch.start()
        self.launch_patch = patch.object(self.window, "_launch", side_effect=self._complete_task)
        self.launch_patch.start()
        self.window.phase = ComparisonPhase.PAUSED
        self.window._update_controls()
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.player_patch.stop()
        self.preview_patch.stop()
        self.launch_patch.stop()
        self.app.processEvents()

    def _preview(self, media: ComparisonMedia, position: float, offset: float, runner: ToolRunner) -> FramePair:
        return FramePair(DecodedFrame(position, self.png), DecodedFrame(position + offset, self.png), offset, position)

    def _complete_task(self, task: Callable[[ToolRunner], object], completed: Callable[[object], None]) -> None:
        completed(task(ToolRunner()))

    def test_mouse_drag_pauses_seeks_both_and_preserves_paused_state(self) -> None:
        slider = self.window.slider
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() // 4, 5))
        self.assertEqual(self.window.phase, ComparisonPhase.SCRUBBING)
        QTest.mouseMove(slider, QPoint(slider.width() * 3 // 4, 5))
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() * 3 // 4, 5))
        self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)
        self.assertAlmostEqual(self.window.position, 7.5, delta=0.1)
        self.session.stop.assert_called()
        self.assertEqual(self.window.timestamps, [self.window.position, self.window.position])

    def test_drag_resumes_playback_and_applies_offset(self) -> None:
        self.window.offset.setValue(1000)
        self.window._play()
        slider = self.window.slider
        QTest.mousePress(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() // 2, 5))
        QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=QPoint(slider.width() // 2, 5))
        self.assertEqual(self.window.phase, ComparisonPhase.PLAYING)
        self.session.start.assert_called_with(self.window.media, self.window.position, 1, 0)

    def test_precise_drag_finishes_before_resuming(self) -> None:
        self.window.precision.blockSignals(True)
        self.window.precision.setCurrentIndex(0)
        self.window.precision.blockSignals(False)
        self.window.phase = ComparisonPhase.PLAYING
        with patch.object(self.window, "_request_frames") as request:
            self.window._scrub_started()
            self.window.slider.setValue(4000)
            self.window._scrub_finished()
            request.assert_called_once_with(resume=True)
            self.assertEqual(self.window.position, 4)
            self.session.start.assert_not_called()

    def test_repeated_precise_seeks_preserve_pending_playback_intent(self) -> None:
        self.window.precision.blockSignals(True)
        self.window.precision.setCurrentIndex(0)
        self.window.precision.blockSignals(False)
        self.window.phase = ComparisonPhase.PLAYING
        with patch.object(self.window, "_launch"):
            self.window.seek(2)
            self.assertEqual(self.window.phase, ComparisonPhase.SEEKING)
            self.assertTrue(self.window._resume_after_seek)
            self.window.seek(3)
            self.assertTrue(self.window._resume_after_seek)
            self.window._scrub_started()
            self.assertTrue(self.window._resume_after_scrub)

    def test_mode_and_switch_shortcut_keep_time_and_playback(self) -> None:
        self.window.position = 4
        self.window.mode.setCurrentIndex(1)
        self.assertFalse(self.window.panes[0].isHidden())
        self.assertTrue(self.window.panes[1].isHidden())
        self.window.activateWindow()
        self.app.processEvents()
        self.window.play_button.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window, Qt.Key.Key_T)
        self.assertTrue(self.window.panes[0].isHidden())
        self.assertFalse(self.window.panes[1].isHidden())
        self.assertEqual(self.window.position, 4)
        self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)

    def test_audio_selection_restarts_only_one_session_with_selected_side(self) -> None:
        self.window._play()
        self.session.start.assert_called_with(self.window.media, self.window.position, 0, 0)
        self.window.audio.setCurrentIndex(1)
        self.session.start.assert_called_with(self.window.media, self.window.position, 0, 1)
        self.window.audio.setCurrentIndex(2)
        self.session.start.assert_called_with(self.window.media, self.window.position, 0, 2)

    def test_empty_overlap_can_be_recovered_by_adjusting_offset(self) -> None:
        self.window.offset.setValue(12000)
        self.assertFalse(self.window.play_button.isEnabled())
        self.assertIn("No common", self.window.status.text())
        self.window.offset.setValue(0)
        self.assertTrue(self.window.play_button.isEnabled())

    def test_stale_worker_result_cannot_change_session(self) -> None:
        self.launch_patch.stop()
        callbacks: list[object] = []
        with patch("gui.comparison.window.ComparisonWorker.start"):
            self.window._launch(lambda _runner: "old", callbacks.append)
            old = self.window._jobs[-1]
            self.window._launch(lambda _runner: "new", callbacks.append)
            old.completed.emit("old")
            self.window._jobs[-1].completed.emit("new")
            self.assertEqual(callbacks, ["new"])
            self.assertTrue(old.runner.cancelled.is_set())

    def test_missing_file_reports_error_and_disables_playback(self) -> None:
        self.launch_patch.stop()
        self.window.load_paths(Path("/missing/comparison-a.mp4"), Path("/missing/comparison-b.mp4"))
        deadline = time.monotonic() + 3
        while self.window.phase == ComparisonPhase.LOADING and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertEqual(self.window.phase, ComparisonPhase.ERROR)
        self.assertIn("comparison-a.mp4", self.window.status.text())
        self.assertFalse(self.window.play_button.isEnabled())

    def test_close_cancels_background_child_before_destroying_window(self) -> None:
        self.launch_patch.stop()
        self.window._launch(lambda runner: runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"]), lambda _result: None)
        job = self.window._jobs[-1]
        QTest.qWait(50)
        start = time.monotonic()
        self.window.close()
        self.assertLess(time.monotonic() - start, 3)
        self.assertFalse(job.isRunning())
        self.assertTrue(job.runner.cancelled.is_set())
        self.assertEqual(self.window.phase, ComparisonPhase.CLOSED)

    def test_normal_mode_recovers_from_static_decode_failure(self) -> None:
        self.window.precision.blockSignals(True)
        self.window.precision.setCurrentIndex(0)
        self.window.precision.blockSignals(False)
        self.window._decode_error("invalid FFmpeg")
        self.assertEqual(self.window.phase, ComparisonPhase.ERROR)
        self.window.precision.setCurrentIndex(1)
        self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)
        self.window.toggle_play()
        self.assertEqual(self.window.phase, ComparisonPhase.PLAYING)

    def test_end_of_common_interval_pauses_both(self) -> None:
        self.window.phase = ComparisonPhase.PLAYING
        self.session.tick.return_value = 10
        self.window._tick()
        self.assertEqual(self.window.phase, ComparisonPhase.PAUSED)
        self.session.stop.assert_called()

    def test_frame_result_exposes_actual_timestamps(self) -> None:
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.red)
        # The PNG path itself is verified with real FFmpeg in the acceptance run;
        # this test checks session application independently of that decoder.
        with patch("gui.comparison.window.QImage.fromData", return_value=image):
            self.window._frames_ready(FramePair(DecodedFrame(1.04, b"a"),
                                                DecodedFrame(1.06, b"b"), 0, 1.055), False, 0, 0)
        self.assertEqual(self.window.timestamps, [1.04, 1.06])
        self.assertIn("00:00:01.040", self.window.panes[0].label.text())
        self.assertEqual(self.window.panes[0].stack.currentWidget(), self.window.panes[0].canvas)

    def test_external_video_workers_wait_for_both_first_frames(self) -> None:
        session = PlaybackSession(self.window)
        media = self.window.media
        assert media is not None
        displayed: list[float] = []
        session.frame_ready.connect(lambda _side, _image, timestamp: displayed.append(timestamp))
        with patch.object(VideoPlaybackWorker, "start"):
            session.start(media, 2, 1, 2)
            first, second = session.workers
            assert isinstance(first, VideoPlaybackWorker) and isinstance(second, VideoPlaybackWorker)
            self.assertEqual(first.command[0], str(media.ffmpeg))
            self.assertEqual(second.command[second.command.index("-ss") + 1], "3.000000")
            first.prepared.emit(QImage(), 12)
            self.assertFalse(session.clock.started.is_set())
            second.prepared.emit(QImage(), 3)
            self.assertTrue(session.clock.started.is_set())
            self.assertEqual(displayed, [])
            session.stop()
            self.assertTrue(first.cancelled.is_set())
            self.assertTrue(second.cancelled.is_set())

    def test_audio_decoding_uses_selected_external_tool_and_one_source(self) -> None:
        session = PlaybackSession(self.window)
        media = ComparisonMedia((Path("a.mp4"), Path("b.mp4")),
                                (VideoDetails(10, 0, 25, has_audio=True),
                                 VideoDetails(10, 0, 30, has_audio=True)),
                                Path("explicit-ffmpeg"), Path("explicit-ffprobe"))
        device = MagicMock()
        device.isNull.return_value = False
        device.isFormatSupported.return_value = True
        with (patch("gui.comparison.playback.QMediaDevices.defaultAudioOutput", return_value=device),
              patch("gui.comparison.playback.QAudioSink"),
              patch.object(VideoPlaybackWorker, "start"), patch.object(AudioPlaybackWorker, "start")):
            session.start(media, 2, 1, 1)
            audio = [worker for worker in session.workers if isinstance(worker, AudioPlaybackWorker)]
            self.assertEqual(len(audio), 1)
            self.assertEqual(audio[0].command[0], "explicit-ffmpeg")
            self.assertEqual(audio[0].command[audio[0].command.index("-i") + 1], "b.mp4")
            self.assertEqual(audio[0].command[audio[0].command.index("-ss") + 1], "3.000000")
            self.assertIn("aresample=async=1:first_pts=0,apad", audio[0].command)
            self.assertEqual(audio[0].command[audio[0].command.index("-t") + 1], "7.000000")
            session.stop()

    def test_audio_session_clock_tracks_processed_pcm_after_tick_stall(self) -> None:
        session = PlaybackSession(self.window)
        session.clock = PlaybackClock(2)
        session.clock.audio_master = True
        session.clock.start()
        sink = MagicMock()
        sink.bytesFree.return_value = 0
        sink.processedUSecs.side_effect = [300000, 300000, 320000]
        with patch.object(session, "sink", sink), patch.object(session, "device", MagicMock()):
            session.buffer = PcmBuffer(8192)
            self.assertEqual(session.tick(), 2.3)
            with patch("gui.comparison.playback.time.monotonic", return_value=100000):
                self.assertEqual(session.tick(), 2.3)
            self.assertEqual(session.tick(), 2.32)
        session.stop()

    def test_audio_write_failure_can_stop_session_synchronously(self) -> None:
        session = PlaybackSession(self.window)
        session.buffer = PcmBuffer(8192)
        session.buffer.put(b"\x00" * 4, threading.Event())
        sink = MagicMock()
        sink.bytesFree.return_value = 4
        device = MagicMock()
        device.write.return_value = -1
        session.failed.connect(session.stop)
        with patch.object(session, "sink", sink), patch.object(session, "device", device):
            self.assertEqual(session.tick(), 0)
            self.assertIsNone(session.sink)
        self.assertEqual(session.workers, [])


if __name__ == "__main__":
    unittest.main()
