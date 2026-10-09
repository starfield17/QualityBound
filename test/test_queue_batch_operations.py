from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from core.i18n import TranslationCatalog
from core.models import (
    AudioMode,
    BackendChoice,
    CodecChoice,
    CompressionMode,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
    MediaInfo,
    QualitySearchResult,
    QualitySearchStatus,
    EncodeResult,
)
from gui.queue_actions import (
    apply_options_to_record,
    apply_output_dir_to_record,
    can_edit_record,
)
from gui.queue_model import QueueColumn, QueueTableModel
from gui.queue_state import QueueItemRecord, QueueSourceDraft, QueueItemStatus, QueueJobSnapshot
from gui.queue_view import create_queue_view


def draft_of(record: QueueItemRecord) -> QueueSourceDraft:
    """Return a record's source draft; a record without one fails the test."""

    assert record.draft is not None
    return record.draft


def override_of(record: QueueItemRecord) -> EncodeOptions:
    """Return the per-item options override an action has just applied."""

    override = draft_of(record).options_override
    assert override is not None
    return override


def _get_qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _create_test_record(
    source_name: str,
    root: Path,
    status: QueueItemStatus = QueueItemStatus.QUEUED,
    target_bitrate_bps: int = 2000000,
    media_duration: float = 60.0,
) -> QueueItemRecord:
    source_path = root / "videos" / f"{source_name}.mov"
    output_path = root / "out" / f"{source_name}.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    media_info = MediaInfo(
        path=source_path,
        duration=media_duration,
        format_bitrate_bps=5000000,
        video_bitrate_bps=5000000,
        audio_bitrate_bps=192000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        pix_fmt="yuv420p",
    )
    plan_item = EncodePlanItem(
        source_path=source_path,
        output_path=output_path,
        media_info=media_info,
        encoder_info=EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="medium",
        ),
        options=EncodeOptions(
            compression_mode=CompressionMode.FIXED_BITRATE,
            ratio=0.5,
            audio_mode=AudioMode.COPY,
        ),
        target_video_bitrate_bps=target_bitrate_bps,
    )
    return QueueItemRecord(
        item_id=f"item-{source_name}",
        plan_item=plan_item,
        job_snapshot=QueueJobSnapshot(
            workdir=root / "workdir",
            ffmpeg_path=root / "ffmpeg",
            ffprobe_path=root / "ffprobe",
            output_root=root / "out",
        ),
        status=status,
        total_passes=1,
    )


class QueueBatchOperationsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _get_qapp()
        repo_root = Path(__file__).resolve().parent.parent
        self.catalog = TranslationCatalog(bundle_dir=repo_root / "config" / "i18n")
        self.tr = self.catalog.translator("en")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.capabilities = {
            "hwaccels": [],
            "codecs": {
                "hevc": [
                    {
                        "backend": "cpu",
                        "encoder": "libx265",
                        "preset_choices": ["slow", "medium", "fast"],
                    }
                ],
                "av1": [
                    {
                        "backend": "cpu",
                        "encoder": "libsvtav1",
                        "preset_choices": ["5", "7"],
                    }
                ],
            },
        }

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _record(
        self,
        name: str,
        status: QueueItemStatus = QueueItemStatus.QUEUED,
    ) -> QueueItemRecord:
        return _create_test_record(name, self.root, status=status)

    def test_can_edit_record_status_filtering(self) -> None:
        rec_queued = self._record("ready", QueueItemStatus.QUEUED)
        rec_analyzing = self._record("analyzing", QueueItemStatus.ANALYZING)
        rec_failed = self._record("failed", QueueItemStatus.FAILED)
        rec_skipped = self._record("skipped", QueueItemStatus.SKIPPED)
        rec_done = self._record("done", QueueItemStatus.DONE)
        rec_decision = self._record("decision", QueueItemStatus.NEEDS_DECISION)
        rec_cancelled = self._record("cancelled", QueueItemStatus.CANCELLED)
        rec_validating = self._record("validating", QueueItemStatus.VALIDATING)
        rec_encoding = self._record("encoding", QueueItemStatus.ENCODING)

        self.assertTrue(can_edit_record(rec_queued))
        self.assertFalse(can_edit_record(rec_analyzing))
        self.assertFalse(can_edit_record(rec_failed))
        self.assertFalse(can_edit_record(rec_cancelled))
        self.assertFalse(can_edit_record(rec_skipped))
        self.assertFalse(can_edit_record(rec_done))
        self.assertFalse(can_edit_record(rec_decision))
        self.assertFalse(can_edit_record(rec_validating))
        self.assertFalse(can_edit_record(rec_encoding))

    def test_apply_options_to_record_fixed_bitrate(self) -> None:
        rec = self._record("test1", QueueItemStatus.QUEUED)
        new_opts = EncodeOptions(
            compression_mode=CompressionMode.FIXED_BITRATE,
            ratio=0.3,
            two_pass=True,
            encoder_preset="fast",
        )
        changed = apply_options_to_record(
            rec, new_opts, runtime_capabilities=self.capabilities
        )
        self.assertTrue(changed)
        self.assertEqual(override_of(rec).ratio, 0.3)
        self.assertEqual(override_of(rec).encoder_preset, "fast")
        self.assertTrue(override_of(rec).two_pass)
        self.assertIsNone(rec.plan_item)
        self.assertEqual(rec.status, QueueItemStatus.AWAITING_START)
        self.assertIsNone(rec.result)

    def test_apply_options_to_record_smart_clears_search_result(self) -> None:
        rec = self._record("test2", QueueItemStatus.WAITING_ANALYSIS)
        rec.bound_plan_item.quality_search_result = QualitySearchResult(
            status=QualitySearchStatus.FOUND,
            encoder_name="libx265",
            backend=BackendChoice.CPU,
        )
        new_opts = EncodeOptions(
            compression_mode=CompressionMode.SMART,
            min_vmaf=93.0,
        )
        changed = apply_options_to_record(
            rec, new_opts, runtime_capabilities=self.capabilities
        )
        self.assertTrue(changed)
        self.assertEqual(override_of(rec).compression_mode, CompressionMode.SMART)
        self.assertEqual(override_of(rec).min_vmaf, 93.0)
        self.assertIsNone(rec.plan_item)
        self.assertEqual(rec.status, QueueItemStatus.AWAITING_START)

    def test_apply_output_dir_to_record(self) -> None:
        rec = self._record("test3", QueueItemStatus.QUEUED)
        new_dir = self.root / "custom" / "output_directory"
        changed = apply_output_dir_to_record(rec, new_dir)
        self.assertTrue(changed)
        self.assertEqual(draft_of(rec).output_dir_override, new_dir.resolve())
        self.assertIsNone(rec.plan_item)

    def test_queue_table_model_batch_actions(self) -> None:
        model = QueueTableModel(self.tr)
        r0 = self._record("file0", QueueItemStatus.QUEUED)
        r1 = self._record("file1", QueueItemStatus.ENCODING)
        r2 = self._record("file2", QueueItemStatus.WAITING_ANALYSIS)
        model.add_records([r0, r1, r2])

        self.assertFalse(model.can_edit_rows([0, 1]))
        self.assertTrue(model.can_edit_rows([0, 2]))

        new_opts = EncodeOptions(
            compression_mode=CompressionMode.FIXED_BITRATE,
            ratio=0.8,
        )
        with self.assertRaisesRegex(RuntimeError, "Every selected"):
            model.apply_options_to_rows(
                [0, 1, 2],
                new_opts,
                runtime_capabilities=self.capabilities,
            )
        self.assertNotEqual(model.records()[0].bound_plan_item.options.ratio, 0.8)
        self.assertNotEqual(model.records()[2].bound_plan_item.options.ratio, 0.8)
        self.assertNotEqual(model.records()[1].bound_plan_item.options.ratio, 0.8)

        updated = model.apply_options_to_rows(
            [0, 2], new_opts, runtime_capabilities=self.capabilities
        )
        self.assertEqual(updated, 2)

        new_dir = self.root / "batch" / "out"
        updated_dirs = model.apply_output_dir_to_rows([0, 2], new_dir)
        self.assertEqual(updated_dirs, 2)
        self.assertEqual(draft_of(model.records()[0]).output_dir_override, new_dir.resolve())
        self.assertEqual(override_of(model.records()[0]).ratio, 0.8)
        self.assertEqual(draft_of(model.records()[2]).output_dir_override, new_dir.resolve())
        self.assertEqual(override_of(model.records()[2]).ratio, 0.8)

    def test_planning_skipped_record_is_excluded_from_output_collision_check(self) -> None:
        model = QueueTableModel(self.tr)
        planned = self._record("clip", QueueItemStatus.QUEUED)
        skipped = self._record("clip_skipped", QueueItemStatus.SKIPPED)
        skipped.bound_plan_item.output_path = planned.output_path
        skipped.bound_plan_item.skip_reason = "probe failed"

        model.add_records([planned, skipped])
        self.assertEqual(model.rowCount(), 2)

        colliding = self._record("clip_other", QueueItemStatus.QUEUED)
        colliding.bound_plan_item.output_path = planned.output_path
        with self.assertRaisesRegex(RuntimeError, "collision"):
            model.add_records([colliding])

    def test_sort_reorders_by_column_and_toggles_direction(self) -> None:
        model = QueueTableModel(self.tr)
        model.add_records([self._record("b"), self._record("a"), self._record("c")])
        self.assertTrue(model.can_sort())

        model.sort(int(QueueColumn.NAME), Qt.SortOrder.AscendingOrder)
        self.assertEqual(
            [model.records()[row].source_path.name for row in range(3)],
            ["a.mov", "b.mov", "c.mov"],
        )

        model.sort(int(QueueColumn.NAME), Qt.SortOrder.DescendingOrder)
        self.assertEqual(
            [model.records()[row].source_path.name for row in range(3)],
            ["c.mov", "b.mov", "a.mov"],
        )

    def test_sort_is_ignored_while_an_item_is_active(self) -> None:
        model = QueueTableModel(self.tr)
        model.add_records([self._record("b"), self._record("a", QueueItemStatus.ENCODING)])
        self.assertFalse(model.can_sort())

        model.sort(int(QueueColumn.NAME), Qt.SortOrder.AscendingOrder)
        self.assertEqual(model.records()[0].source_path.name, "b.mov")

    def test_header_click_sorts_the_model(self) -> None:
        model = QueueTableModel(self.tr)
        model.add_records([self._record("b"), self._record("a")])
        view = create_queue_view()
        self.addCleanup(view.deleteLater)
        view.setModel(model)

        view.horizontalHeader().sectionClicked.emit(int(QueueColumn.NAME))

        self.assertEqual(model.records()[0].source_path.name, "a.mov")
        self.assertEqual(view.horizontalHeader().sortIndicatorSection(), int(QueueColumn.NAME))

    def test_codec_override_defers_binding_and_clears_smart_result(self) -> None:
        rec = self._record("switch", QueueItemStatus.WAITING_ANALYSIS)
        rec.bound_plan_item.quality_search_result = QualitySearchResult(
            status=QualitySearchStatus.FOUND,
            encoder_name="libx265",
            backend=BackendChoice.CPU,
        )
        options = EncodeOptions(
            codec=CodecChoice.AV1,
            backend=BackendChoice.CPU,
            compression_mode=CompressionMode.SMART,
            encoder_preset="5",
        )

        self.assertTrue(
            apply_options_to_record(
                rec, options, runtime_capabilities=self.capabilities
            )
        )
        self.assertIsNone(rec.plan_item)
        self.assertIsNone(rec.job_snapshot)
        self.assertEqual(override_of(rec).codec, CodecChoice.AV1)
        self.assertEqual(override_of(rec).encoder_preset, "5")
        self.assertIsNone(rec.plan_item)
        self.assertEqual(rec.total_passes, 1)

    def test_needs_decision_edit_is_rejected_without_stranding_file(self) -> None:
        rec = self._record("decision", QueueItemStatus.NEEDS_DECISION)
        rejected = self.root / "out" / "decision.size-miss-test.mp4"
        rejected.write_bytes(b"preserved")
        rec.result = EncodeResult(
            source_path=rec.source_path,
            output_path=rec.output_path,
            success=False,
            needs_decision=True,
            rejected_output_path=rejected,
        )

        self.assertFalse(
            apply_options_to_record(
                rec, EncodeOptions(), runtime_capabilities=self.capabilities
            )
        )
        self.assertTrue(rejected.exists())
        self.assertIsNotNone(rec.result)

    def test_batch_option_override_defers_output_validation_until_start(self) -> None:
        model = QueueTableModel(self.tr)
        first = self._record("same", QueueItemStatus.QUEUED)
        second = self._record("other", QueueItemStatus.WAITING_ANALYSIS)
        second.bound_plan_item.source_path = self.root / "other" / "same.mov"
        model.add_records([first, second])
        self.assertEqual(model.apply_options_to_rows([0, 1], EncodeOptions(ratio=0.4)), 2)
        for record in model.records():
            self.assertIsNone(record.plan_item)
            self.assertEqual(override_of(record).ratio, 0.4)
            self.assertEqual(record.status, QueueItemStatus.AWAITING_START)

    def test_option_override_does_not_require_capability_snapshot(self) -> None:
        rec = self._record("no-capabilities", QueueItemStatus.QUEUED)
        self.assertTrue(apply_options_to_record(rec, EncodeOptions(), runtime_capabilities=None))
        self.assertIsNone(rec.plan_item)
        self.assertIsNotNone(draft_of(rec).options_override)

    def test_capability_snapshot_reconfiguration_never_probes_ffmpeg(self) -> None:
        rec = self._record("amf", QueueItemStatus.QUEUED)
        capabilities = {
            "hwaccels": [],
            "codecs": {
                "hevc": [
                    {
                        "backend": "amf",
                        "encoder": "hevc_amf",
                        "preset_choices": ["speed", "quality"],
                    }
                ],
                "av1": [],
            },
        }
        with (
            patch("core.ffmpeg.encoders.preset_choices_for_encoder") as encoder_probe,
            patch("core.encoding.planning.preset_choices_for_encoder") as planning_probe,
        ):
            changed = apply_options_to_record(
                rec,
                EncodeOptions(
                    backend=BackendChoice.AMF,
                    compression_mode=CompressionMode.FIXED_BITRATE,
                ),
                runtime_capabilities=capabilities,
            )

        self.assertTrue(changed)
        encoder_probe.assert_not_called()
        planning_probe.assert_not_called()
        self.assertIsNone(rec.plan_item)
        self.assertEqual(override_of(rec).backend, BackendChoice.AMF)

    def test_failed_and_cancelled_records_are_terminal_for_batch_edits(self) -> None:
        model = QueueTableModel(self.tr)
        failed = self._record("failed-terminal", QueueItemStatus.FAILED)
        cancelled = self._record("cancelled-terminal", QueueItemStatus.CANCELLED)
        model.add_records([failed, cancelled])

        self.assertFalse(model.can_edit_rows([0]))
        self.assertFalse(model.can_edit_rows([1]))
        with self.assertRaisesRegex(RuntimeError, "Every selected"):
            model.apply_output_dir_to_rows([0, 1], self.root / "new-output")

    def test_output_override_does_not_create_directories_before_start(self) -> None:
        model = QueueTableModel(self.tr)
        first = self._record("same", QueueItemStatus.QUEUED)
        second = self._record("other", QueueItemStatus.WAITING_ANALYSIS)
        second.bound_plan_item.source_path = self.root / "other" / "same.mov"
        second.bound_plan_item.output_path = self.root / "other-output" / "same.mp4"
        model.add_records([first, second])
        candidate_output_dir = self.root / "not-created" / "nested"

        self.assertEqual(model.apply_output_dir_to_rows([0, 1], candidate_output_dir), 2)
        self.assertFalse(candidate_output_dir.exists())
        for record in model.records():
            self.assertIsNone(record.plan_item)
            self.assertEqual(draft_of(record).output_dir_override, candidate_output_dir.resolve())


if __name__ == "__main__":
    unittest.main(verbosity=2)
