from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.models import (
    BackendChoice,
    CodecChoice,
    EncodeOptions,
    EncodePlanItem,
    EncodeResult,
    EncoderInfo,
    QualityUnreachablePolicy,
    OperationCancelledError,
    SkippedOutputOutcome,
    SkipOrigin,
    SkippedOutputPolicy,
)
from core.media.skipped import (
    group_skipped_output_pairs,
    is_eligible_skipped_item,
    publish_skipped_source,
    publish_skipped_sources,
)


def _item(root: Path, *, skip_reason: str | None = None, overwrite: bool = True) -> EncodePlanItem:
    source = root / "source.mov"
    source.write_bytes(b"source-bytes")
    return EncodePlanItem(
        source_path=source,
        output_path=root / "out" / "source_hevc.mp4",
        media_info=None,
        encoder_info=EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="slow",
        ),
        options=EncodeOptions(
            overwrite=overwrite,
            quality_unreachable_policy=QualityUnreachablePolicy.SKIP,
        ),
        skip_reason=skip_reason,
    )


class SkippedOutputPublishTestCase(unittest.TestCase):
    def test_copy_cancel_keeps_destination_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = _item(Path(directory))
            item.output_path.parent.mkdir()
            item.output_path.write_bytes(b"existing")
            with self.assertRaises(OperationCancelledError):
                publish_skipped_source(item, cancel_check=lambda: True)
            self.assertEqual(item.output_path.read_bytes(), b"existing")
            self.assertFalse(list(item.output_path.parent.glob(".*.copy-*.tmp")))

    def test_late_output_is_not_overwritten_and_failure_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            item = _item(Path(directory), overwrite=False)
            result = EncodeResult(item.source_path, item.output_path, False,
                                  skipped=True, skip_origin=SkipOrigin.SMART_PREDICTED_OVERSIZE)
            def late_output(*_args):
                item.output_path.write_bytes(b"late-output")
            with patch("core.media.skipped.shutil.copystat", side_effect=late_output):
                published = publish_skipped_source(item, result)
            self.assertFalse(published.copied)
            self.assertEqual(item.output_path.read_bytes(), b"late-output")
            self.assertEqual(result.skipped_output_outcome, SkippedOutputOutcome.FAILED)
            self.assertFalse(result.skipped)
            self.assertFalse(result.success)
            self.assertFalse(list(item.output_path.parent.glob(".*.copy-*.tmp")))

    def test_analysis_skip_is_copied_to_planned_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=False,
                skipped=True,
                skip_origin=SkipOrigin.SMART_ANALYSIS,
            )
            self.assertTrue(is_eligible_skipped_item(item, result))
            published = publish_skipped_source(item)
            self.assertTrue(published.copied)
            self.assertEqual(item.output_path.read_bytes(), b"source-bytes")

    def test_planning_skip_is_not_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            item = _item(Path(temp_dir), skip_reason="probe failed")
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=False,
                skipped=True,
                skip_origin=SkipOrigin.PLANNING,
                error_message="probe failed",
            )
            self.assertFalse(is_eligible_skipped_item(item, result))
            self.assertEqual(publish_skipped_sources([(item, result)]), [])
            self.assertFalse(item.output_path.exists())

    def test_existing_output_without_overwrite_is_left_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root, overwrite=False)
            item.output_path.parent.mkdir()
            item.output_path.write_bytes(b"existing")
            published = publish_skipped_source(item)
            self.assertFalse(published.copied)
            self.assertEqual(published.reason, "output exists")
            self.assertEqual(published.source_path, item.source_path)
            self.assertEqual(published.output_path, item.output_path)
            self.assertEqual(item.output_path.read_bytes(), b"existing")

    def test_ignore_policy_does_not_change_eligibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            item = _item(Path(temp_dir))
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=False,
                skipped=True,
                skip_origin=SkipOrigin.SMART_ANALYSIS,
            )
            self.assertTrue(is_eligible_skipped_item(item, result))

    def test_skipped_outputs_are_grouped_by_each_item_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for name in ("copy", "ask", "ignore", "planning"):
                (root / name).mkdir()
            copy_item = _item(root / "copy")
            ask_item = _item(root / "ask")
            ignore_item = _item(root / "ignore")
            ask_item.options.skipped_output_policy = SkippedOutputPolicy.ASK
            ignore_item.options.skipped_output_policy = SkippedOutputPolicy.IGNORE
            result = EncodeResult(
                source_path=copy_item.source_path,
                output_path=copy_item.output_path,
                success=False,
                skipped=True,
                skip_origin=SkipOrigin.SMART_ANALYSIS,
            )
            grouped = group_skipped_output_pairs(
                [
                    (copy_item, result),
                    (ask_item, result),
                    (ignore_item, result),
                    (_item(root / "planning", skip_reason="probe failed"), result),
                ]
            )

            self.assertEqual(len(grouped[SkippedOutputPolicy.COPY]), 1)
            self.assertEqual(len(grouped[SkippedOutputPolicy.ASK]), 1)
            self.assertEqual(len(grouped[SkippedOutputPolicy.IGNORE]), 1)

    def test_only_smart_analysis_skip_origins_are_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            item = _item(Path(temp_dir))
            for origin in (
                SkipOrigin.SMART_ANALYSIS,
                SkipOrigin.SMART_ANALYSIS_DECISION,
            ):
                with self.subTest(origin=origin):
                    result = EncodeResult(
                        source_path=item.source_path,
                        output_path=item.output_path,
                        success=False,
                        skipped=True,
                        skip_origin=origin,
                    )
                    self.assertTrue(is_eligible_skipped_item(item, result))

            for origin in (
                None,
                SkipOrigin.PLANNING,
                SkipOrigin.SIZE_MISS_DISCARD,
            ):
                with self.subTest(origin=origin):
                    result = EncodeResult(
                        source_path=item.source_path,
                        output_path=item.output_path,
                        success=False,
                        skipped=True,
                        skip_origin=origin,
                    )
                    self.assertFalse(is_eligible_skipped_item(item, result))


if __name__ == "__main__":
    unittest.main()
