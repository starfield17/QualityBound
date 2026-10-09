from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.i18n import TranslationCatalog
from core.models import (
    BackendChoice,
    CodecChoice,
    CompressionMode,
    ConstraintFailureKind,
    DecisionActionCode,
    EncodeOptions,
    EncodePlanItem,
    EncodeResult,
    EncoderInfo,
    MediaInfo,
    QualitySearchStatus,
    SegmentedAnalysisResult,
    SmartAlgorithm,
)
from gui.queue_model import QueueTableModel
from gui.queue_state import (
    QueueItemRecord,
    QueueItemStatus,
    QueueJobSnapshot,
    build_tags,
    build_tooltip,
    compute_metrics,
)


def _get_qapp() -> QApplication:
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        app = QApplication([])
    return app


def _record(
    root: Path,
    name: str,
    status: QueueItemStatus,
    *,
    duration: float = 60.0,
    segmented: SegmentedAnalysisResult | None = None,
    result: EncodeResult | None = None,
) -> QueueItemRecord:
    source = root / f"{name}.mov"
    source.write_bytes(b"source")
    output = root / f"{name}.mp4"
    item = EncodePlanItem(
        source_path=source,
        output_path=output,
        media_info=MediaInfo(
            path=source,
            duration=duration,
            format_bitrate_bps=2_000_000,
            video_bitrate_bps=1_800_000,
            audio_bitrate_bps=128_000,
            width=1280,
            height=720,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
        ),
        encoder_info=EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="slow",
        ),
        options=EncodeOptions(
            compression_mode=CompressionMode.SMART,
            smart_algorithm=SmartAlgorithm.V2_EXPERIMENTAL,
            overwrite=True,
        ),
        segmented_analysis_result=segmented,
    )
    return QueueItemRecord(
        item_id=name,
        plan_item=item,
        job_snapshot=QueueJobSnapshot(root / "work", root / "ffmpeg", root / "ffprobe", root),
        status=status,
        total_passes=2,
        result=result,
    )


class QueueFeedbackTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _get_qapp()
        repo_root = Path(__file__).resolve().parent.parent
        cls.catalog = TranslationCatalog(bundle_dir=repo_root / "config" / "i18n")

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def test_segmented_analysis_exposes_and_applies_quality_decision(self) -> None:
        segmented = SegmentedAnalysisResult(
            status=QualitySearchStatus.CONSTRAINT_UNSATISFIED,
            encoder_name="libx265",
            backend=BackendChoice.CPU,
            failure_kind=ConstraintFailureKind.QUALITY_UNREACHABLE,
            reason="quality target unreachable at the size limit",
        )
        result = EncodeResult(
            source_path=self.root / "segmented.mov",
            output_path=self.root / "segmented.mp4",
            success=False,
            needs_decision=True,
            segmented_analysis_result=segmented,
        )
        record = _record(
            self.root,
            "segmented",
            QueueItemStatus.NEEDS_DECISION,
            segmented=segmented,
            result=result,
        )
        model = QueueTableModel(self.catalog.translator("en"))
        model.add_records([record])

        options = model.decision_options_for_row(0)
        self.assertIn(DecisionActionCode.SKIP, [option.action_code for option in options])

        skip = next(option for option in options if option.action_code == DecisionActionCode.SKIP)
        self.assertTrue(model.apply_quality_decision(0, skip))
        self.assertEqual(record.status, QueueItemStatus.SKIPPED)
        self.assertIsNotNone(record.result)
        assert record.result is not None
        self.assertTrue(record.result.skipped)

    def test_can_resolve_row_requires_a_result_payload(self) -> None:
        model = QueueTableModel(self.catalog.translator("en"))
        without_result = _record(self.root, "no-result", QueueItemStatus.NEEDS_DECISION)
        with_result = _record(
            self.root,
            "with-result",
            QueueItemStatus.NEEDS_DECISION,
            segmented=SegmentedAnalysisResult(
                QualitySearchStatus.CONSTRAINT_UNSATISFIED,
                "libx265",
                BackendChoice.CPU,
                failure_kind=ConstraintFailureKind.MEDIA_BUDGET_TOO_SMALL,
            ),
            result=EncodeResult(
                source_path=self.root / "with-result.mov",
                output_path=self.root / "with-result.mp4",
                success=False,
                needs_decision=True,
            ),
        )
        model.add_records([without_result, with_result])

        self.assertFalse(model.can_resolve_row(0))
        self.assertTrue(model.can_resolve_row(1))

    def test_tags_and_tooltips_follow_the_active_language(self) -> None:
        segmented = SegmentedAnalysisResult(
            QualitySearchStatus.CONSTRAINT_UNSATISFIED,
            "libx265",
            BackendChoice.CPU,
            shots=[],
            failure_kind=ConstraintFailureKind.SIZE_BLOCKED,
            reason="needs a wider size budget",
            approximate=True,
        )
        record = _record(
            self.root,
            "localized",
            QueueItemStatus.NEEDS_DECISION,
            segmented=segmented,
        )
        zh = self.catalog.translator("zh_cn")

        tags = build_tags(record, zh)
        self.assertEqual(
            tags,
            [
                zh.t("gui.tag.two_pass"),
                zh.t("gui.tag.overwrite"),
                zh.t("gui.tag.external_subtitles"),
                zh.t("gui.tag.decision"),
            ],
        )
        english_tags = build_tags(record, self.catalog.translator("en"))
        self.assertEqual(english_tags, ["Two-pass", "Overwrite", "ExtSub", "Decision"])

        tooltip = build_tooltip(record, zh)
        self.assertIn(zh.t("gui.tooltip.source", path=record.source_path), tooltip)
        self.assertIn(zh.t("gui.tooltip.smart_analysis", reason="needs a wider size budget"), tooltip)
        self.assertNotIn("Source:", tooltip)

    def test_clear_completed_removes_failed_but_keeps_decisions(self) -> None:
        model = QueueTableModel(self.catalog.translator("en"))
        failed = _record(self.root, "failed", QueueItemStatus.FAILED)
        decision = _record(self.root, "decision", QueueItemStatus.NEEDS_DECISION)
        model.add_records([failed, decision])

        self.assertEqual(model.clear_completed(), 1)
        self.assertEqual([record.item_id for record in model.records()], [decision.item_id])

    def test_terminal_failures_count_toward_queue_progress(self) -> None:
        failed = _record(self.root, "failed", QueueItemStatus.FAILED, duration=30.0)
        failed.file_progress = 0.0
        cancelled = _record(self.root, "cancelled", QueueItemStatus.CANCELLED, duration=30.0)
        cancelled.file_progress = 0.0

        metrics = compute_metrics([failed, cancelled])

        self.assertEqual(metrics.queue_percent, 100.0)
        self.assertEqual(metrics.completed_items, 0)
        self.assertEqual(metrics.failed_items, 1)
        self.assertEqual(metrics.cancelled_items, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
