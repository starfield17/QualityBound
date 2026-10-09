from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt, QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from core.encoding import prepare_encode_requests
from core.i18n import get_translator
from core.models import (
    BackendChoice, CodecChoice, CompressionMode, ContainerChoice, EncodeOptions,
    EncodeRequest, MediaInfo, VideoFileItem,
)
from gui.gui_workers import QueueIntakeWorker
from gui.gui_mainwindow import MainWindow
from gui.queue_manager import QueueExecuteWorker, QueueExecutionItem
from gui.queue_actions import apply_options_to_record, apply_output_dir_to_record
from gui.queue_model import QueueColumn, QueueTableModel
from gui.queue_state import QueueSourceDraft, QueueItemStatus, create_draft_record, unbind_for_retry


def media(path: Path) -> MediaInfo:
    return MediaInfo(path, 10, 2_000_000, 1_800_000, 128_000, 1280, 720, 30, "h264", "aac")


CAPABILITIES = {"hwaccels": [], "codecs": {
    "hevc": [{"backend": "cpu", "encoder": "libx265", "preset_choices": ["slow"]}],
    "av1": [{"backend": "cpu", "encoder": "libsvtav1", "preset_choices": ["5"]}],
}}


class DeferredQueueTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source.mov"
        self.source.write_bytes(b"video")
        self.record = create_draft_record(QueueSourceDraft(VideoFileItem(self.source, Path("source.mov")), None, media(self.source)))
        self.model = QueueTableModel(get_translator("en", Path(__file__).resolve().parent.parent / "config"))
        self.model.add_records([self.record])

    def prepare(self, requests: list[EncodeRequest]):
        with (
            patch("core.encoding.planning.discover_ffmpeg_tools", return_value=(self.root / "ffmpeg", self.root / "ffprobe")),
            patch("core.encoding.planning.ensure_encoder_capabilities", return_value=CAPABILITIES),
            patch("core.encoding.planning.probe_media_info", side_effect=lambda _tool, path, **_kw: media(path)),
        ):
            return prepare_encode_requests(requests, workdir=self.root / "work")

    def request(self, options: EncodeOptions, output: Path | None = None) -> EncodeRequest:
        return EncodeRequest(self.record.draft.file_item, options, output, self.record.draft.input_root)

    def test_added_file_has_no_execution_binding_or_final_output(self) -> None:
        self.assertEqual(self.record.status, QueueItemStatus.AWAITING_START)
        self.assertIsNone(self.record.plan_item)
        self.assertIsNone(self.record.job_snapshot)
        self.assertEqual(self.record.duration_sec, 10)
        for column in QueueColumn:
            self.model.data(self.model.index(0, column), Qt.ItemDataRole.DisplayRole)
            self.model.sort(column)
        self.assertIsNone(self.model.metrics().estimated_saved_bytes)
        self.assertFalse(self.model.execution_records())

    def test_start_uses_new_codec_container_backend_and_bitrate(self) -> None:
        options = EncodeOptions(codec=CodecChoice.AV1, container=ContainerChoice.MKV,
                                backend=BackendChoice.CPU, encoder_preset="5",
                                compression_mode=CompressionMode.FIXED_BITRATE, ratio=0.4,
                                copy_external_subtitles=False)
        output = self.root / "new-output"
        plan = self.prepare([self.request(options, output)])
        self.model.apply_prepared_plan([self.record.item_id], plan, self.root / "work")
        bound = self.model.records()[0]
        self.assertEqual(bound.output_path, output / "source_av1.mkv")
        self.assertEqual(bound.plan_item.encoder_info.encoder_name, "libsvtav1")
        self.assertEqual(bound.plan_item.target_video_bitrate_bps, 720_000)
        self.assertEqual(bound.status, QueueItemStatus.QUEUED)
        options.ratio = 0.9
        self.assertEqual(bound.plan_item.options.ratio, 0.4)

    def test_custom_options_and_directory_are_independent_drafts(self) -> None:
        options = EncodeOptions(backend=BackendChoice.NVENC, two_pass=True)
        self.assertTrue(apply_options_to_record(self.record, options))
        self.assertTrue(apply_output_dir_to_record(self.record, self.root / "custom"))
        options.two_pass = False
        self.assertTrue(self.record.draft.options_override.two_pass)
        self.assertEqual(self.record.draft.output_dir_override, self.root / "custom")
        self.assertIsNone(self.record.plan_item)
        self.model.restore_global_options([0])
        self.assertIsNone(self.model.records()[0].draft.options_override)
        self.assertIsNotNone(self.model.records()[0].draft.output_dir_override)
        self.model.restore_global_output([0])
        self.assertIsNone(self.model.records()[0].draft.output_dir_override)

    def test_invalid_old_backend_does_not_affect_intake(self) -> None:
        worker = QueueIntakeWorker(None, False, None, [self.record.draft.file_item])
        received = []
        worker.completed.connect(received.append)
        with (
            patch("gui.gui_workers.find_binary", return_value=self.root / "ffprobe"),
            patch("gui.gui_workers.probe_media_info", return_value=media(self.source)),
            patch("gui.gui_workers.prepare_encode_requests") as build,
        ):
            worker.run()
        self.assertEqual(len(received[0]), 1)
        build.assert_not_called()
        self.assertFalse((self.root / "work").exists())

    def test_manual_retry_returns_to_draft_and_preserves_overrides(self) -> None:
        options = EncodeOptions(backend=BackendChoice.CPU)
        self.record.draft.options_override = copy.deepcopy(options)
        plan = self.prepare([self.request(options)])
        self.model.apply_prepared_plan([self.record.item_id], plan, self.root / "work")
        bound = self.model.records()[0]
        bound.status = QueueItemStatus.FAILED
        unbind_for_retry(bound)
        self.assertEqual(bound.status, QueueItemStatus.AWAITING_START)
        self.assertIsNone(bound.plan_item)
        self.assertIsNone(bound.job_snapshot)
        self.assertEqual(bound.draft.options_override.backend, BackendChoice.CPU)

    def test_batch_preparation_accepts_distinct_per_item_options(self) -> None:
        other = self.root / "other.mov"
        other.write_bytes(b"video")
        plan = self.prepare([
            self.request(EncodeOptions(backend=BackendChoice.CPU)),
            EncodeRequest(VideoFileItem(other, Path("other.mov")), EncodeOptions(codec=CodecChoice.AV1, backend=BackendChoice.CPU), self.root / "custom"),
        ])
        self.assertEqual([item.encoder_info.encoder_name for item in plan.items], ["libx265", "libsvtav1"])
        self.assertEqual(plan.items[1].output_path, self.root / "custom" / "other_av1.mp4")
        self.assertIsNot(plan.items[0].options, plan.items[1].options)

    def test_output_collision_with_bound_record_does_not_partially_bind(self) -> None:
        plan = self.prepare([self.request(EncodeOptions(backend=BackendChoice.CPU))])
        from gui.queue_state import create_queue_records
        self.model.add_records(create_queue_records(plan, self.root / "work"))
        with self.assertRaisesRegex(RuntimeError, "collision"):
            self.model.apply_prepared_plan([self.record.item_id], plan, self.root / "work")
        self.assertIsNone(self.model.records()[0].plan_item)

    def test_folder_layout_is_retained_until_start(self) -> None:
        directory = self.root / "folder"
        source = directory / "sub" / "clip.mov"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"video")
        request = EncodeRequest(VideoFileItem(source, Path("sub/clip.mov")), EncodeOptions(backend=BackendChoice.CPU), None, directory)
        plan = self.prepare([request])
        self.assertEqual(plan.items[0].output_path, self.root / "folder_compressed_hevc" / "sub" / "clip_hevc.mp4")

    def test_prepare_lock_blocks_edit_and_decision_resume(self) -> None:
        from gui.queue_manager import QueueManager
        from PySide6.QtCore import QModelIndex
        another = copy.deepcopy(self.record)
        another.item_id = "another"
        self.model.add_records([another])
        manager = QueueManager(self.model)
        manager.begin_preparation()
        self.assertFalse(manager.start())
        self.assertFalse(self.model.can_sort())
        self.assertFalse(self.model.moveRows(QModelIndex(), 0, 1, QModelIndex(), 2))
        self.assertEqual(self.model.remove_rows_by_index([0]), 0)
        with self.assertRaisesRegex(RuntimeError, "preparation"):
            self.model.apply_options_to_rows([0], EncodeOptions())
        manager.finish_preparation()
        self.assertTrue(self.model.can_sort())

    def test_window_start_snapshots_current_options_and_custom_output(self) -> None:
        self._window_preparation(cancel=False, close=False)

    def test_cancel_after_plan_return_does_not_commit_or_start(self) -> None:
        self._window_preparation(cancel=True, close=False)

    def test_close_during_preparation_does_not_commit_or_start(self) -> None:
        self._window_preparation(cancel=True, close=True)

    def _window_preparation(self, *, cancel: bool, close: bool) -> None:
        with patch("core.config.store.app_config_path", return_value=self.root / "config.json"):
            window = MainWindow(Path(__file__).resolve().parent.parent, language="en")
        window.queue_model.add_records([copy.deepcopy(self.record)])
        options = EncodeOptions(codec=CodecChoice.AV1, backend=BackendChoice.CPU, container=ContainerChoice.MKV)
        output = self.root / "new"
        window.output_edit.setText(str(output))
        plan = self.prepare([self.request(options, output)])
        release = threading.Event()
        captured = []

        def prepare(requests, **_kwargs):
            captured.extend(copy.deepcopy(requests))
            if not release.wait(3):
                raise RuntimeError("Test preparation was not released.")
            return plan

        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        try:
            with (
                patch.object(window.options_panel, "read_options", return_value=options),
                patch("gui.gui_workers.prepare_encode_requests", side_effect=prepare),
                patch.object(window.queue_manager, "start", return_value=True) as start,
                patch("gui.gui_mainwindow.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes),
            ):
                window._start_queue()
                worker = window.active_worker
                worker.finished.connect(loop.quit)
                self.assertTrue(window.queue_manager.is_busy())
                self.assertFalse(window.queue_model.can_sort())
                window._start_queue()
                with patch.object(window, "_resolve_queue_decision") as decide:
                    window._on_queue_row_activated(window.table_view, window.queue_model.index(0, 0))
                    decide.assert_not_called()
                options.codec = CodecChoice.HEVC
                if close:
                    window.close()
                elif cancel:
                    window._stop_active_task()
                release.set()
                timer.start(5000)
                loop.exec()
                timer.stop()
                self.assertFalse(worker.isRunning())
                self.assertEqual(captured[0].options.codec, CodecChoice.AV1)
                if cancel:
                    start.assert_not_called()
                    self.assertIsNone(window.queue_model.records()[0].plan_item)
                else:
                    start.assert_called_once_with(max_workers=1)
                    self.assertEqual(window.queue_model.records()[0].output_path, output / "source_av1.mkv")
                self.assertFalse(window.queue_manager.is_busy())
        finally:
            release.set()
            if window.active_worker is not None:
                window.active_worker.cancel()
                window.active_worker.wait(5000)
                self.app.processEvents()
            window.queue_manager.abandon_run()
            window._close_after_running_task_stops = False
            window.close()

    def test_mixed_tool_contexts_analyze_all_before_encoding(self) -> None:
        from gui.queue_state import create_queue_records
        plan = self.prepare([self.request(EncodeOptions(backend=BackendChoice.CPU))])
        first = create_queue_records(plan, self.root / "work-a")[0]
        second = copy.deepcopy(first)
        second.item_id = "second"
        second.job_snapshot.ffmpeg_path = self.root / "other-ffmpeg"
        second.job_snapshot.workdir = self.root / "work-b"
        worker = QueueExecuteWorker([QueueExecutionItem(first.item_id, first), QueueExecutionItem(second.item_id, second)], 2)
        events = []

        def analyze(tool, items, workdir, **_kwargs):
            events.append(("analyze", tool, workdir))
            return [None] * len(items)

        def encode(bound, workdir, **kwargs):
            self.assertEqual(kwargs["max_workers"], 2)
            self.assertEqual(kwargs["analysis_results"], [None])
            events.append(("encode", bound.ffmpeg_path, workdir))
            return [object()]

        with (
            patch("gui.queue_manager.run_analysis_phase", side_effect=analyze),
            patch("gui.queue_manager.execute_plan_concurrent", side_effect=encode),
        ):
            worker.run()
        self.assertEqual([event[0] for event in events], ["analyze", "analyze", "encode", "encode"])
        self.assertEqual(events[0][1:], events[2][1:])
        self.assertEqual(events[1][1:], events[3][1:])

    def test_existing_output_fails_only_that_item_at_start(self) -> None:
        output = self.root / "source_hevc.mp4"
        output.write_bytes(b"existing")
        other = self.root / "other.mov"
        other.write_bytes(b"video")
        plan = self.prepare([
            self.request(EncodeOptions(backend=BackendChoice.CPU)),
            EncodeRequest(VideoFileItem(other, Path("other.mov")), EncodeOptions(backend=BackendChoice.CPU)),
        ])
        self.assertIn("already exists", plan.items[0].skip_reason)
        self.assertIsNone(plan.items[1].skip_reason)
        self.model.apply_prepared_plan([self.record.item_id], type(plan)([plan.items[0]], plan.ffmpeg_path, plan.ffprobe_path, plan.input_root, plan.output_root), self.root / "work")
        self.assertEqual(self.model.records()[0].status, QueueItemStatus.FAILED)
        self.assertEqual(output.read_bytes(), b"existing")

    def test_explicit_probe_pair_honors_selected_ffmpeg(self) -> None:
        worker = QueueIntakeWorker(None, False, None, [self.record.draft.file_item], ffmpeg_path="selected-ffmpeg")
        with (
            patch("gui.gui_workers.discover_ffmpeg_tools", return_value=(self.root / "ffmpeg", self.root / "paired-ffprobe")) as discover,
            patch("gui.gui_workers.find_binary") as fallback,
            patch("gui.gui_workers.probe_media_info", return_value=media(self.source)) as probe,
        ):
            worker.run()
        discover.assert_called_once_with("selected-ffmpeg", None)
        fallback.assert_not_called()
        self.assertEqual(probe.call_args.args[0], self.root / "paired-ffprobe")

    def test_resume_keeps_bound_parameters_and_does_not_read_globals(self) -> None:
        plan = self.prepare([self.request(EncodeOptions(backend=BackendChoice.CPU, min_vmaf=91))])
        self.model.apply_prepared_plan([self.record.item_id], plan, self.root / "work")
        with patch("core.config.store.app_config_path", return_value=self.root / "config.json"):
            window = MainWindow(Path(__file__).resolve().parent.parent, language="en")
        window.queue_model.add_records(self.model.records())
        try:
            with (
                patch.object(window.options_panel, "read_options", side_effect=AssertionError("Resume must use its snapshot")),
                patch.object(window.queue_manager, "start", return_value=True),
            ):
                window._start_queue()
            self.assertEqual(window.queue_model.records()[0].plan_item.options.min_vmaf, 91)
        finally:
            window.close()

    def test_prepared_analysis_is_not_run_again_and_rejects_wrong_identity(self) -> None:
        from core.encoding import execute_plan, execute_plan_concurrent
        from core.models import EncodeResult
        options = EncodeOptions(backend=BackendChoice.CPU, compression_mode=CompressionMode.FIXED_BITRATE)
        plan = self.prepare([self.request(options)])
        terminal = EncodeResult(self.source, plan.items[0].output_path, False, skipped=True)
        for concurrent in (False, True):
            runner = execute_plan_concurrent if concurrent else execute_plan
            kwargs = {"max_workers": 2} if concurrent else {}
            with patch("core.encoding.parallel.run_analysis_phase" if concurrent else "core.encoding.executor.run_analysis_phase") as analyze:
                result = runner(plan, self.root / "work", analysis_results=[terminal], **kwargs)
                self.assertEqual(result, [terminal])
                analyze.assert_not_called()
                wrong = copy.deepcopy(terminal)
                wrong.source_path = self.root / "wrong.mov"
                with self.assertRaisesRegex(ValueError, "identity"):
                    runner(plan, self.root / "work", analysis_results=[wrong], **kwargs)

    def test_mixed_context_decision_callback_and_pause_keep_item_identity(self) -> None:
        from core.models import EncodeResult
        from gui.queue_state import create_queue_records
        plan = self.prepare([self.request(EncodeOptions(backend=BackendChoice.CPU))])
        first = create_queue_records(plan, self.root / "work-a")[0]
        second = copy.deepcopy(first)
        second.item_id = "second"
        second.plan_item.source_path = self.root / "second.mov"
        second.plan_item.output_path = self.root / "second.mp4"
        second.job_snapshot.ffmpeg_path = self.root / "other-ffmpeg"
        worker = QueueExecuteWorker([QueueExecutionItem(first.item_id, first), QueueExecutionItem(second.item_id, second)], 2)
        received, paused = [], []
        worker.item_finished.connect(lambda item_id, result: received.append((item_id, result)))
        worker.paused.connect(lambda: paused.append(True))
        terminal = EncodeResult(second.source_path, second.output_path, False, needs_decision=True)

        def analyze(tool, items, workdir, **kwargs):
            if tool == second.job_snapshot.ffmpeg_path:
                kwargs["item_result_callback"](0, terminal)
                worker.pause_after_current()
                return [terminal]
            return [None]

        def encode(bound, workdir, **kwargs):
            self.assertTrue(kwargs["pause_check"]())
            return [result for result in kwargs["analysis_results"] if result is not None]

        with (
            patch("gui.queue_manager.run_analysis_phase", side_effect=analyze),
            patch("gui.queue_manager.execute_plan_concurrent", side_effect=encode),
        ):
            worker.run()
        self.assertEqual(received, [("second", terminal)])
        self.assertEqual(paused, [True])
        self.assertIsNone(first.plan_item.quality_search_result)


if __name__ == "__main__":
    unittest.main()
