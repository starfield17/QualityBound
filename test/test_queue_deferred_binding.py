from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.encoding import prepare_encode_requests
from core.i18n import get_translator
from core.models import (
    BackendChoice, CodecChoice, CompressionMode, ContainerChoice, EncodeOptions,
    EncodeRequest, MediaInfo, VideoFileItem,
)
from gui.gui_workers import QueueIntakeWorker
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
            patch("gui.gui_workers.build_encode_plan") as build,
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


if __name__ == "__main__":
    unittest.main()
