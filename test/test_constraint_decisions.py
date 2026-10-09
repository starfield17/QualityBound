from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from core.smart.v1.receipts import (
    ANALYSIS_RECEIPT_SCHEMA_VERSION,
    analysis_receipt_path,
    load_analysis_receipt,
    save_analysis_receipt,
)
from core.smart.v1.decisions import prepare_size_miss_retry
from core.encoding import analyze_plan_item, item_needs_smart_analysis
from core.models import (
    AnalysisReceipt,
    BackendChoice,
    CodecChoice,
    ConstraintFailureKind,
    DecisionActionCode,
    EncodeOptions,
    EncodePlanItem,
    EncodeResult,
    EncoderInfo,
    MediaInfo,
    QualityCandidateResult,
    QualitySearchResult,
    QualitySearchStatus,
    QualityUnreachablePolicy,
    SkipOrigin,
    SizeBlockedPolicy,
    VmafBackend,
    VmafRuntimeSupport,
)
from core.config.store import _default_app_config, smart_policies_from_config
from core.smart.v1.decisions import apply_decision_to_options, build_decision_options
from core.smart.v1.workflow import analyze_quality
from core.smart.v1.cache import measurement_configuration_fingerprint, quality_configuration_fingerprint
from core.smart.v1.bitrate import reselect_from_candidates
from core.smart.v1.sampling.planner import PlannedWindow, SamplePlan
from core.smart.v1.sampling.scout import SamplingResult
from core.i18n import get_translator
from gui.queue_model import QueueTableModel
from gui.queue_manager import QueueManager
from gui.queue_state import QueueItemRecord, QueueItemStatus, compute_metrics, mark_finished
from gui.queue_state import QueueJobSnapshot


def _item(root: Path) -> EncodePlanItem:
    source = root / "source.mp4"
    with source.open("wb") as fh:
        fh.truncate(100_000_000)
    options = EncodeOptions(
        codec=CodecChoice.HEVC,
        min_vmaf=95.0,
        max_output_ratio=0.10,
        min_video_kbps=250,
    )
    return EncodePlanItem(
        source_path=source,
        output_path=root / "output.mp4",
        media_info=MediaInfo(
            path=source,
            duration=60.0,
            format_bitrate_bps=4_000_000,
            video_bitrate_bps=3_000_000,
            audio_bitrate_bps=128_000,
            width=1920,
            height=1080,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
            audio_stream_count=1,
        ),
        encoder_info=EncoderInfo(
            codec=CodecChoice.HEVC,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="slow",
        ),
        options=options,
    )


def _three_window_sampling() -> SamplingResult:
    windows = tuple(
        PlannedWindow(
            id=f"search:test-{index}",
            start_sec=float(index * 20),
            duration_sec=5.0,
            reasons=("test",),
        )
        for index in range(3)
    )
    return SamplingResult(SamplePlan((), windows, (), False), ())


def _candidates() -> list[QualityCandidateResult]:
    return [
        QualityCandidateResult(video_bitrate_bps=1_000_000, min_vmaf=94.0),
        QualityCandidateResult(video_bitrate_bps=1_500_000, min_vmaf=96.0),
    ]


class ConstraintDecisionTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_size_blocked_result_exposes_concrete_local_choices(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            item = _item(Path(temp_dir))
            result = reselect_from_candidates(_candidates(), item)

            self.assertEqual(result.status, QualitySearchStatus.CONSTRAINT_UNSATISFIED)
            self.assertEqual(result.failure_kind, ConstraintFailureKind.SIZE_BLOCKED)
            self.assertEqual(result.selected_video_bitrate_bps, 1_500_000)
            self.assertIsNotNone(result.predicted_output_bytes)
            actions = {option.action_code: option for option in build_decision_options(result)}
            self.assertIn(DecisionActionCode.RELAX_SIZE, actions)
            self.assertIn(DecisionActionCode.RELAX_QUALITY, actions)
            self.assertFalse(actions[DecisionActionCode.RELAX_SIZE].requires_analysis)

            relaxed = replace(item, options=apply_decision_to_options(item.options, actions[DecisionActionCode.RELAX_SIZE]))
            reselected = reselect_from_candidates(result.candidates, relaxed)
            self.assertEqual(reselected.status, QualitySearchStatus.FOUND)
            self.assertEqual(reselected.selected_video_bitrate_bps, 1_500_000)

    def test_cached_candidates_outside_configured_bitrate_bounds_are_not_selectable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            item = _item(Path(temp_dir))
            item.options.min_video_kbps = 1_000
            below_minimum = reselect_from_candidates(
                [QualityCandidateResult(video_bitrate_bps=500_000, min_vmaf=99.0)],
                item,
            )
            self.assertEqual(below_minimum.status, QualitySearchStatus.CONSTRAINT_UNSATISFIED)

            item.options.min_video_kbps = 250
            item.options.max_video_kbps = 1_000
            above_maximum = reselect_from_candidates(
                [QualityCandidateResult(video_bitrate_bps=1_500_000, min_vmaf=99.0)],
                item,
            )
            self.assertEqual(above_maximum.status, QualitySearchStatus.CONSTRAINT_UNSATISFIED)

    def test_size_miss_retry_invalidates_old_successful_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            quality = reselect_from_candidates(
                [QualityCandidateResult(video_bitrate_bps=1_500_000, min_vmaf=96.0)],
                replace(item, options=replace(item.options, max_output_ratio=1.0)),
            )
            item.quality_search_result = quality
            rejected = root / "output.size-miss-test.mp4"
            rejected.write_bytes(b"encoded")
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=False,
                needs_decision=True,
                rejected_output_path=rejected,
                actual_output_bytes=120_000_000,
                allowed_output_bytes=100_000_000,
            )

            corrected = prepare_size_miss_retry(item, result)

            self.assertEqual(corrected, item.options.max_video_kbps * 1_000)
            self.assertEqual(item.target_video_bitrate_bps, corrected)
            self.assertIsNone(item.quality_search_result)
            self.assertTrue(item_needs_smart_analysis(item))

    def test_size_blocked_relax_size_encodes_without_asking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            item.options.size_blocked_policy = SizeBlockedPolicy.RELAX_SIZE
            blocked = reselect_from_candidates(_candidates(), item)
            with patch("core.encoding.analysis.analyze_quality", return_value=blocked):
                terminal = analyze_plan_item(root / "ffmpeg", item, root)
            self.assertIsNone(terminal)
            self.assertEqual(item.quality_search_result.status, QualitySearchStatus.FOUND)
            self.assertEqual(item.target_video_bitrate_bps, 1_500_000)

    def test_size_blocked_ask_still_needs_a_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            item.options.size_blocked_policy = SizeBlockedPolicy.ASK
            blocked = reselect_from_candidates(_candidates(), item)
            with patch("core.encoding.analysis.analyze_quality", return_value=blocked):
                terminal = analyze_plan_item(root / "ffmpeg", item, root)
            self.assertIsNotNone(terminal)
            assert terminal is not None
            self.assertTrue(terminal.needs_decision)
            self.assertFalse(terminal.skipped)

    def test_lower_quality_policy_can_resolve_oversize_before_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _item(root)
            item.options.size_blocked_policy = SizeBlockedPolicy.RELAX_QUALITY
            blocked = reselect_from_candidates([
                QualityCandidateResult(video_bitrate_bps=1_000_000, min_vmaf=94.0),
                QualityCandidateResult(video_bitrate_bps=16_000_000, min_vmaf=96.0),
            ], item)
            self.assertGreater(blocked.required_output_ratio, 1.0)
            with patch("core.encoding.analysis.analyze_quality", return_value=blocked):
                terminal = analyze_plan_item(root / "ffmpeg", item, root)
            self.assertIsNone(terminal)
            self.assertTrue(item.quality_search_result.success)
            self.assertEqual(item.options.min_vmaf, 94.0)

    def test_predicted_oversize_skip_after_policy_selection(self) -> None:
        for policy in SizeBlockedPolicy:
            for ratio in (None, 1.0, 1.01):
                if policy != SizeBlockedPolicy.ASK and ratio != 1.01:
                    continue
                with self.subTest(policy=policy, ratio=ratio), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    item = _item(root)
                    item.options.size_blocked_policy = policy
                    blocked = QualitySearchResult(
                        status=QualitySearchStatus.CONSTRAINT_UNSATISFIED,
                        encoder_name=item.encoder_info.encoder_name,
                        backend=item.encoder_info.backend,
                        failure_kind=ConstraintFailureKind.SIZE_BLOCKED,
                        required_output_ratio=ratio,
                        reason="Measured quality-passing candidate exceeds the limit.",
                    )
                    with patch("core.encoding.analysis.analyze_quality", return_value=blocked):
                        terminal = analyze_plan_item(root / "ffmpeg", item, root)
                    self.assertIsNotNone(terminal)
                    self.assertEqual(terminal.skipped, ratio is not None and ratio > 1.0)
                    self.assertEqual(terminal.needs_decision, not terminal.skipped)


    def test_unsupported_analysis_is_failed_not_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            assert item.encoder_info is not None
            unsupported = QualitySearchResult(
                status=QualitySearchStatus.UNSUPPORTED,
                encoder_name=item.encoder_info.encoder_name,
                backend=item.encoder_info.backend,
                reason="libvmaf unavailable",
            )
            with patch("core.encoding.analysis.analyze_quality", return_value=unsupported):
                terminal = analyze_plan_item(root / "ffmpeg", item, root)

            self.assertIsNotNone(terminal)
            assert terminal is not None
            self.assertFalse(terminal.success)
            self.assertFalse(terminal.skipped)
            self.assertFalse(terminal.needs_decision)

    def test_quality_unreachable_skips_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            item.options.quality_unreachable_policy = QualityUnreachablePolicy.SKIP
            unreachable = reselect_from_candidates(
                [QualityCandidateResult(video_bitrate_bps=1_000_000, min_vmaf=90.0)],
                item,
            )
            self.assertEqual(unreachable.failure_kind, ConstraintFailureKind.QUALITY_UNREACHABLE)
            with patch("core.encoding.analysis.analyze_quality", return_value=unreachable):
                terminal = analyze_plan_item(root / "ffmpeg", item, root)
            self.assertIsNotNone(terminal)
            assert terminal is not None
            self.assertTrue(terminal.skipped)
            self.assertFalse(terminal.needs_decision)
            self.assertEqual(terminal.skip_origin, SkipOrigin.SMART_ANALYSIS)

    def test_quality_unreachable_ask_needs_a_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            item.options.quality_unreachable_policy = QualityUnreachablePolicy.ASK
            unreachable = reselect_from_candidates(
                [QualityCandidateResult(video_bitrate_bps=1_000_000, min_vmaf=90.0)],
                item,
            )
            with patch("core.encoding.analysis.analyze_quality", return_value=unreachable):
                terminal = analyze_plan_item(root / "ffmpeg", item, root)
            self.assertIsNotNone(terminal)
            assert terminal is not None
            self.assertTrue(terminal.needs_decision)
            self.assertFalse(terminal.skipped)

    def test_old_app_config_backfills_smart_policies(self) -> None:
        data = {"language": "en"}
        for key, value in _default_app_config().items():
            data.setdefault(key, value)
        size_policy, unreachable_policy, skipped_policy = smart_policies_from_config(data)
        self.assertEqual(size_policy, SizeBlockedPolicy.RELAX_SIZE)
        self.assertEqual(unreachable_policy, QualityUnreachablePolicy.SKIP)
        self.assertEqual(skipped_policy.value, "copy")
        empty_size, empty_unreachable, empty_skipped = smart_policies_from_config({})
        self.assertEqual(empty_size, SizeBlockedPolicy.RELAX_SIZE)
        self.assertEqual(empty_unreachable, QualityUnreachablePolicy.SKIP)
        self.assertEqual(empty_skipped.value, "copy")

    def test_policy_changes_do_not_invalidate_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"ffmpeg")
            item = _item(root)
            measurement = measurement_configuration_fingerprint(ffmpeg, item)
            decision = quality_configuration_fingerprint(ffmpeg, item)

            item.options.min_vmaf = 92.0
            item.options.max_output_ratio = 0.20

            self.assertEqual(measurement_configuration_fingerprint(ffmpeg, item), measurement)
            self.assertNotEqual(quality_configuration_fingerprint(ffmpeg, item), decision)

    def test_needs_decision_is_not_counted_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            record = QueueItemRecord(
                item_id="one",
                plan_item=item,
                job_snapshot=QueueJobSnapshot(root, root / "ffmpeg", root / "ffprobe", root),
                status=QueueItemStatus.ANALYZING,
                total_passes=1,
            )
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=False,
                needs_decision=True,
                quality_search_result=reselect_from_candidates(_candidates(), item),
            )
            mark_finished(record, result)
            metrics = compute_metrics([record])

            self.assertEqual(record.status, QueueItemStatus.NEEDS_DECISION)
            self.assertEqual(metrics.needs_decision_items, 1)
            self.assertEqual(metrics.completed_items, 0)
            self.assertLess(metrics.queue_percent, 100.0)

    def test_finished_smart_result_syncs_effective_constraints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            record = QueueItemRecord(
                item_id="one",
                plan_item=item,
                job_snapshot=QueueJobSnapshot(root, root / "ffmpeg", root / "ffprobe", root),
                status=QueueItemStatus.ANALYZING,
                total_passes=1,
            )
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=True,
                effective_min_vmaf=91.5,
                effective_max_output_ratio=0.42,
            )

            mark_finished(record, result)

            self.assertEqual(record.plan_item.options.min_vmaf, 91.5)
            self.assertEqual(record.plan_item.options.max_output_ratio, 0.42)

    def test_queue_manager_reconciles_idle_after_last_decision_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            record = QueueItemRecord(
                item_id="one",
                plan_item=item,
                job_snapshot=QueueJobSnapshot(root, root / "ffmpeg", root / "ffprobe", root),
                status=QueueItemStatus.SKIPPED,
                total_passes=1,
                result=EncodeResult(
                    source_path=item.source_path,
                    output_path=item.output_path,
                    success=False,
                    skipped=True,
                ),
            )
            model = QueueTableModel(get_translator("en", Path(__file__).resolve().parent.parent / "config"))
            model.add_records([record])
            manager = QueueManager(model)
            states: list[str] = []
            manager.stateChanged.connect(states.append)

            manager.reconcile_after_decision()

            self.assertEqual(states, ["idle"])

    def test_queue_applies_a_local_quality_decision_without_reanalysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"ffmpeg")
            item = _item(root)
            quality = reselect_from_candidates(
                _candidates(),
                item,
                measurement_fingerprint=measurement_configuration_fingerprint(ffmpeg, item),
                fingerprint=quality_configuration_fingerprint(ffmpeg, item),
            )
            item.quality_search_result = quality
            record = QueueItemRecord(
                item_id="one",
                plan_item=item,
                job_snapshot=QueueJobSnapshot(root, ffmpeg, root / "ffprobe", root),
                status=QueueItemStatus.ANALYZING,
                total_passes=1,
            )
            mark_finished(
                record,
                EncodeResult(
                    source_path=item.source_path,
                    output_path=item.output_path,
                    success=False,
                    needs_decision=True,
                    quality_search_result=quality,
                ),
            )
            model = QueueTableModel(get_translator("en", Path(__file__).resolve().parent.parent / "config"))
            model.add_records([record])
            relax_size = next(
                option
                for option in model.decision_options_for_row(0)
                if option.action_code == DecisionActionCode.RELAX_SIZE
            )

            self.assertTrue(model.apply_quality_decision(0, relax_size))
            self.assertEqual(record.status, QueueItemStatus.WAITING_ANALYSIS)
            self.assertEqual(record.plan_item.quality_search_result.status, QualitySearchStatus.FOUND)

    def test_queue_analysis_skip_decision_records_eligible_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            quality = reselect_from_candidates(_candidates(), item)
            item.quality_search_result = quality
            record = QueueItemRecord(
                item_id="one",
                plan_item=item,
                job_snapshot=QueueJobSnapshot(root, root / "ffmpeg", root / "ffprobe", root),
                status=QueueItemStatus.NEEDS_DECISION,
                total_passes=1,
                result=EncodeResult(
                    source_path=item.source_path,
                    output_path=item.output_path,
                    success=False,
                    needs_decision=True,
                    quality_search_result=quality,
                ),
            )
            model = QueueTableModel(get_translator("en", Path(__file__).resolve().parent.parent / "config"))
            model.add_records([record])
            skip = next(
                option
                for option in model.decision_options_for_row(0)
                if option.action_code == DecisionActionCode.SKIP
            )

            self.assertTrue(model.apply_quality_decision(0, skip))
            assert record.result is not None
            self.assertEqual(record.result.skip_origin, SkipOrigin.SMART_ANALYSIS_DECISION)

    def test_queue_can_accept_a_preserved_size_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            item.source_path.with_suffix(".srt").write_text("subtitle", encoding="utf-8")
            item.output_path.write_bytes(b"old")
            rejected = root / "output.size-miss-abcd.mp4"
            rejected.write_bytes(b"new")
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=False,
                needs_decision=True,
                rejected_output_path=rejected,
                actual_output_bytes=800,
                allowed_output_bytes=700,
            )
            record = QueueItemRecord(
                item_id="one",
                plan_item=item,
                job_snapshot=QueueJobSnapshot(root, root / "ffmpeg", root / "ffprobe", root),
                status=QueueItemStatus.NEEDS_DECISION,
                total_passes=1,
                result=result,
            )
            model = QueueTableModel(get_translator("en", Path(__file__).resolve().parent.parent / "config"))
            model.add_records([record])

            self.assertTrue(model.accept_size_miss(0))
            self.assertEqual(item.output_path.read_bytes(), b"new")
            self.assertFalse(rejected.exists())
            self.assertEqual(record.status, QueueItemStatus.DONE)
            self.assertEqual(item.output_path.with_suffix(".srt").read_text(encoding="utf-8"), "subtitle")

    def test_queue_size_miss_discard_records_non_analysis_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _item(root)
            rejected = root / "output.size-miss-abcd.mp4"
            rejected.write_bytes(b"encoded")
            result = EncodeResult(
                source_path=item.source_path,
                output_path=item.output_path,
                success=False,
                needs_decision=True,
                rejected_output_path=rejected,
            )
            record = QueueItemRecord(
                item_id="one",
                plan_item=item,
                job_snapshot=QueueJobSnapshot(root, root / "ffmpeg", root / "ffprobe", root),
                status=QueueItemStatus.NEEDS_DECISION,
                total_passes=1,
                result=result,
            )
            model = QueueTableModel(get_translator("en", Path(__file__).resolve().parent.parent / "config"))
            model.add_records([record])

            self.assertTrue(model.discard_size_miss(0))
            self.assertEqual(result.skip_origin, SkipOrigin.SIZE_MISS_DISCARD)
            self.assertFalse(rejected.exists())


class AnalysisReceiptTestCase(unittest.TestCase):
    def test_receipt_round_trips_and_corruption_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            fingerprint = "a" * 64
            receipt = AnalysisReceipt(
                schema_version=ANALYSIS_RECEIPT_SCHEMA_VERSION,
                measurement_fingerprint=fingerprint,
                source_identity={"path": "source.mp4", "size": 10, "mtime_ns": 20},
                ffmpeg_identity={"path": "ffmpeg", "size": 30, "mtime_ns": 40},
                encoder_identity={"encoder": "libx265", "backend": "cpu"},
                sample_scheme_version=5,
                sample_windows=[(10.0, 5.0)],
                scout_windows=[
                    {
                        "id": "scout-001",
                        "start_sec": 10.0,
                        "duration_sec": 2.0,
                        "si_p90": 0.7,
                        "ti_p90": 0.8,
                        "si_rank": 0.5,
                        "ti_rank": 0.5,
                        "difficulty": 0.75,
                        "scene_cut_times": [],
                        "scene_cut_count": 0,
                        "max_scene_score": 0.0,
                    }
                ],
                search_windows=[
                    {
                        "id": "search:scout-001",
                        "start_sec": 10.0,
                        "duration_sec": 5.0,
                        "reasons": ["global_hardest"],
                        "scout_id": "scout-001",
                        "crosses_scene_cut": False,
                    }
                ],
                holdout_windows=[
                    {
                        "id": "holdout:scout-002",
                        "start_sec": 20.0,
                        "duration_sec": 5.0,
                        "reasons": ["holdout_difficulty_and_diversity"],
                        "scout_id": "scout-002",
                        "crosses_scene_cut": False,
                    }
                ],
                refinement_rounds=[
                    {"round": 1, "promoted_windows": ["20.0:5.0"], "resolved": True}
                ],
                search_min_vmaf=94.0,
                holdout_min_vmaf=95.0,
                search_fingerprint="b" * 64,
                measurement_configuration={
                    "vmaf_backend": "cpu",
                    "vmaf_subsample": 1,
                },
                candidates=[
                    QualityCandidateResult(
                        video_bitrate_bps=1_000_000,
                        min_vmaf=94.0,
                        segment_vmaf=[94.0],
                    ),
                    QualityCandidateResult(
                        video_bitrate_bps=1_500_000,
                        min_vmaf=96.0,
                        segment_vmaf=[96.0],
                    ),
                ],
                created_at="2026-08-17T00:00:00+00:00",
            )
            path = save_analysis_receipt(root, receipt)
            loaded = load_analysis_receipt(root, fingerprint)

            self.assertEqual(path, analysis_receipt_path(root, fingerprint))
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.candidates[1].min_vmaf, 96.0)
            self.assertEqual(loaded.sample_scheme_version, 5)
            self.assertEqual(len(loaded.scout_windows), 1)
            self.assertEqual(loaded.search_windows[0]["reasons"], ["global_hardest"])
            self.assertEqual(
                loaded.holdout_windows[0]["reasons"],
                ["holdout_difficulty_and_diversity"],
            )
            self.assertEqual(loaded.refinement_rounds[0]["round"], 1)
            self.assertEqual(loaded.search_min_vmaf, 94.0)
            self.assertEqual(loaded.holdout_min_vmaf, 95.0)

            inconsistent = json.loads(path.read_text(encoding="utf-8"))
            inconsistent["candidates"][1]["min_vmaf"] = 99.0
            path.write_text(json.dumps(inconsistent), encoding="utf-8")
            self.assertIsNone(load_analysis_receipt(root, fingerprint))

            out_of_range = json.loads(json.dumps(asdict(receipt)))
            out_of_range["candidates"][1]["min_vmaf"] = 101.0
            out_of_range["candidates"][1]["segment_vmaf"] = [101.0]
            path.write_text(json.dumps(out_of_range), encoding="utf-8")
            self.assertIsNone(load_analysis_receipt(root, fingerprint))

            inconsistent_windows = json.loads(json.dumps(asdict(receipt)))
            inconsistent_windows["sample_windows"][0][0] = 11.0
            path.write_text(json.dumps(inconsistent_windows), encoding="utf-8")
            self.assertIsNone(load_analysis_receipt(root, fingerprint))

            missing_search_windows = json.loads(json.dumps(asdict(receipt)))
            missing_search_windows["search_windows"] = []
            path.write_text(json.dumps(missing_search_windows), encoding="utf-8")
            self.assertIsNone(load_analysis_receipt(root, fingerprint))

            overlapping_windows = json.loads(json.dumps(asdict(receipt)))
            overlapping_windows["holdout_windows"][0]["start_sec"] = 12.0
            path.write_text(json.dumps(overlapping_windows), encoding="utf-8")
            self.assertIsNone(load_analysis_receipt(root, fingerprint))

            path.write_text("{not-json", encoding="utf-8")
            self.assertIsNone(load_analysis_receipt(root, fingerprint))

    def test_receipt_path_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                analysis_receipt_path(Path(temp_dir), "../outside")

    def test_analyze_quality_reuses_receipt_after_policy_only_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"ffmpeg")
            item = _item(root)

            def score(_ffmpeg, _item, _references, bitrate, *_args, **_kwargs):
                vmaf = 96.0 if bitrate >= 800_000 else 92.0
                return QualityCandidateResult(
                    video_bitrate_bps=bitrate,
                    min_vmaf=vmaf,
                    segment_vmaf=[vmaf, vmaf, vmaf],
                    observed_video_bitrate_bps=bitrate,
                )

            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(
                        VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                    ),
                ),
                patch("core.smart.v1.workflow.discover_sample_plan", return_value=_three_window_sampling()),
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate", side_effect=score) as first_score,
            ):
                progress_events: list[dict[str, object]] = []
                first = analyze_quality(
                    ffmpeg,
                    item,
                    root,
                    root / "analysis.log",
                    progress_callback=progress_events.append,
                )

            self.assertEqual(first.status, QualitySearchStatus.FOUND)
            self.assertGreater(first_score.call_count, 0)
            candidate_events = [
                event for event in progress_events if event.get("state") == "candidate_finished"
            ]
            for tier in {event["candidate_tier"] for event in candidate_events}:
                tier_events = [event for event in candidate_events if event["candidate_tier"] == tier]
                self.assertEqual(tier_events[0]["candidate_index"], 1)
                self.assertTrue(
                    all(
                        int(event["candidate_index"]) <= int(event["candidate_limit"])
                        for event in tier_events
                    )
                )
            receipt = load_analysis_receipt(root, first.measurement_fingerprint)
            self.assertIsNotNone(receipt)

            changed_policy = replace(
                item,
                options=replace(item.options, min_vmaf=93.0),
                quality_search_result=None,
            )
            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(
                        VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                    ),
                ),
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate", side_effect=score) as second_score,
            ):
                second = analyze_quality(ffmpeg, changed_policy, root, root / "analysis-2.log")

            self.assertEqual(second.status, QualitySearchStatus.FOUND)
            self.assertEqual(second.measurement_fingerprint, first.measurement_fingerprint)
            self.assertNotEqual(second.fingerprint, first.fingerprint)
            self.assertGreater(second_score.call_count, 0)
            first_bitrates = {candidate.video_bitrate_bps for candidate in first.candidates}
            second_bitrates = {candidate.video_bitrate_bps for candidate in second.candidates}
            self.assertTrue(first_bitrates.issubset(second_bitrates))


if __name__ == "__main__":
    unittest.main()
