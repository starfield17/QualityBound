from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

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
from gui.queue_model import QueueTableModel
from gui.queue_state import QueueItemRecord, QueueItemStatus, QueueJobSnapshot


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
        rec_draft = self._record("draft", QueueItemStatus.DRAFT)
        rec_failed = self._record("failed", QueueItemStatus.FAILED)
        rec_skipped = self._record("skipped", QueueItemStatus.SKIPPED)
        rec_done = self._record("done", QueueItemStatus.DONE)
        rec_decision = self._record("decision", QueueItemStatus.NEEDS_DECISION)
        rec_cancelled = self._record("cancelled", QueueItemStatus.CANCELLED)
        rec_running = self._record("running", QueueItemStatus.RUNNING)
        rec_encoding = self._record("encoding", QueueItemStatus.ENCODING)

        self.assertTrue(can_edit_record(rec_queued))
        self.assertFalse(can_edit_record(rec_draft))
        self.assertFalse(can_edit_record(rec_failed))
        self.assertFalse(can_edit_record(rec_cancelled))
        self.assertFalse(can_edit_record(rec_skipped))
        self.assertFalse(can_edit_record(rec_done))
        self.assertFalse(can_edit_record(rec_decision))
        self.assertFalse(can_edit_record(rec_running))
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
        self.assertEqual(rec.plan_item.options.ratio, 0.3)
        self.assertEqual(rec.plan_item.options.encoder_preset, "fast")
        self.assertEqual(rec.total_passes, 2)
        # 5000000 bps * 0.3 = 1500000 bps
        self.assertEqual(rec.plan_item.target_video_bitrate_bps, 1500000)
        self.assertEqual(rec.status, QueueItemStatus.QUEUED)
        self.assertIsNone(rec.result)

    def test_apply_options_to_record_smart_clears_search_result(self) -> None:
        rec = self._record("test2", QueueItemStatus.WAITING_ANALYSIS)
        rec.plan_item.quality_search_result = QualitySearchResult(
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
        self.assertEqual(rec.plan_item.options.compression_mode, CompressionMode.SMART)
        self.assertEqual(rec.plan_item.options.min_vmaf, 93.0)
        self.assertIsNone(rec.plan_item.quality_search_result)
        self.assertEqual(rec.status, QueueItemStatus.WAITING_ANALYSIS)

    def test_apply_output_dir_to_record(self) -> None:
        rec = self._record("test3", QueueItemStatus.QUEUED)
        new_dir = self.root / "custom" / "output_directory"
        changed = apply_output_dir_to_record(rec, new_dir)
        self.assertTrue(changed)
        self.assertEqual(rec.plan_item.output_path, new_dir.resolve() / "test3.mp4")

    def test_queue_table_model_batch_actions(self) -> None:
        model = QueueTableModel(self.tr)
        r0 = self._record("file0", QueueItemStatus.QUEUED)
        r1 = self._record("file1", QueueItemStatus.RUNNING)
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
        self.assertNotEqual(model.record_for_row(0).plan_item.options.ratio, 0.8)
        self.assertNotEqual(model.record_for_row(2).plan_item.options.ratio, 0.8)
        self.assertNotEqual(model.record_for_row(1).plan_item.options.ratio, 0.8)

        updated = model.apply_options_to_rows(
            [0, 2], new_opts, runtime_capabilities=self.capabilities
        )
        self.assertEqual(updated, 2)

        new_dir = self.root / "batch" / "out"
        updated_dirs = model.apply_output_dir_to_rows([0, 2], new_dir)
        self.assertEqual(updated_dirs, 2)
        self.assertEqual(model.record_for_row(0).plan_item.output_path, new_dir.resolve() / "file0_hevc.mp4")
        self.assertEqual(model.record_for_row(2).plan_item.output_path, new_dir.resolve() / "file2_hevc.mp4")

    def test_planning_skipped_record_is_excluded_from_output_collision_check(self) -> None:
        model = QueueTableModel(self.tr)
        planned = self._record("clip", QueueItemStatus.QUEUED)
        skipped = self._record("clip_skipped", QueueItemStatus.SKIPPED)
        skipped.plan_item.output_path = planned.output_path
        skipped.plan_item.skip_reason = "probe failed"

        model.add_records([planned, skipped])
        self.assertEqual(model.rowCount(), 2)

        colliding = self._record("clip_other", QueueItemStatus.QUEUED)
        colliding.plan_item.output_path = planned.output_path
        with self.assertRaisesRegex(RuntimeError, "collision"):
            model.add_records([colliding])

    def test_codec_change_rebinds_encoder_and_clears_smart_result(self) -> None:
        rec = self._record("switch", QueueItemStatus.WAITING_ANALYSIS)
        rec.plan_item.quality_search_result = QualitySearchResult(
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
        assert rec.plan_item.encoder_info is not None
        self.assertEqual(rec.plan_item.encoder_info.encoder_name, "libsvtav1")
        self.assertEqual(rec.output_path.name, "switch_av1.mp4")
        self.assertIsNone(rec.plan_item.quality_search_result)
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

    def test_output_collision_rolls_back_entire_batch(self) -> None:
        model = QueueTableModel(self.tr)
        first = self._record("same", QueueItemStatus.QUEUED)
        second = self._record("other", QueueItemStatus.WAITING_ANALYSIS)
        second.plan_item.source_path = self.root / "other" / "same.mov"
        model.add_records([first, second])
        original_paths = [record.output_path for record in model.records()]

        with self.assertRaisesRegex(RuntimeError, "collision"):
            model.apply_options_to_rows(
                [0, 1],
                EncodeOptions(
                    compression_mode=CompressionMode.FIXED_BITRATE,
                    ratio=0.4,
                ),
                runtime_capabilities=self.capabilities,
            )

        self.assertEqual(
            [record.output_path for record in model.records()], original_paths
        )

    def test_reconfiguration_requires_capability_snapshot(self) -> None:
        rec = self._record("no-capabilities", QueueItemStatus.QUEUED)
        with self.assertRaisesRegex(RuntimeError, "capabilities are not ready"):
            apply_options_to_record(rec, EncodeOptions(), runtime_capabilities=None)

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
        assert rec.plan_item.encoder_info is not None
        self.assertEqual(rec.plan_item.encoder_info.encoder_name, "hevc_amf")

    def test_failed_and_cancelled_records_are_terminal_for_batch_edits(self) -> None:
        model = QueueTableModel(self.tr)
        failed = self._record("failed-terminal", QueueItemStatus.FAILED)
        cancelled = self._record("cancelled-terminal", QueueItemStatus.CANCELLED)
        model.add_records([failed, cancelled])

        self.assertFalse(model.can_edit_rows([0]))
        self.assertFalse(model.can_edit_rows([1]))
        with self.assertRaisesRegex(RuntimeError, "Every selected"):
            model.apply_output_dir_to_rows([0, 1], self.root / "new-output")

    def test_failed_atomic_edit_does_not_create_candidate_directories(self) -> None:
        model = QueueTableModel(self.tr)
        first = self._record("same", QueueItemStatus.QUEUED)
        second = self._record("other", QueueItemStatus.WAITING_ANALYSIS)
        second.plan_item.source_path = self.root / "other" / "same.mov"
        second.plan_item.output_path = self.root / "other-output" / "same.mp4"
        model.add_records([first, second])
        candidate_output_dir = self.root / "not-created" / "nested"

        with self.assertRaisesRegex(RuntimeError, "collision"):
            model.apply_output_dir_to_rows([0, 1], candidate_output_dir)

        self.assertFalse(candidate_output_dir.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
