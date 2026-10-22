from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.smart.v1.receipts import load_analysis_receipt
from core.smart.v1.runtime import AnalysisTier, VmafBackend, build_analysis_execution_plan
from core.models import (
    BackendChoice,
    ConstraintFailureKind,
    DecisionActionCode,
    EncodeOptions,
    EncodePlanItem,
    MediaInfo,
    QualityCandidateResult,
    QualitySearchStatus,
    SizePrediction,
    VmafRuntimeSupport,
)
from core.smart.v1.workflow import analyze_quality
from core.smart.v1.decisions import build_decision_options
from core.progress_events import ProgressEvent
from core.smart.v1.sampling.planner import SamplePlanningError
from core.smart.v1.sampling.complexity import ComplexityProbeError

from _analysis_runtime_fixtures import (
    _analysis_item,
    _capabilities,
    _encoder,
    _sampling_with_holdout,
    _three_window_sampling,
)


class SmartWorkflowTestCase(unittest.TestCase):
    def test_analyze_quality_persists_only_exact_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"x" * 100_000_000)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = EncodePlanItem(
                source_path=source,
                output_path=root / "out.mp4",
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
                    pix_fmt="yuv420p",
                    color_transfer="bt709",
                ),
                encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
                options=EncodeOptions(encoder_preset="slow"),
            )
            seen_tiers: list[str] = []

            def score(*_args, **kwargs):
                plan = kwargs.get("plan")
                bitrate = _args[3]
                if plan is not None:
                    seen_tiers.append(plan.tier.value)
                    if plan.tier is AnalysisTier.COARSE:
                        return QualityCandidateResult(
                            video_bitrate_bps=bitrate,
                            min_vmaf=96.0 if bitrate >= 800_000 else 90.0,
                            segment_vmaf=[96.0, 96.0, 96.0] if bitrate >= 800_000 else [90.0],
                            observed_video_bitrate_bps=bitrate,
                            predicted_output_bytes=bitrate // 10,
                        )
                return QualityCandidateResult(
                    video_bitrate_bps=bitrate,
                    min_vmaf=95.5 if bitrate >= 900_000 else 91.0,
                    segment_vmaf=[96.0, 95.5, 96.2] if bitrate >= 900_000 else [91.0, 91.0, 91.0],
                    observed_video_bitrate_bps=bitrate,
                    predicted_output_bytes=bitrate // 10,
                )

            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(
                        VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                    ),
                ),
                patch("core.smart.v1.workflow.detect_analysis_capabilities", return_value=_capabilities()),
                patch("core.smart.v1.workflow.discover_sample_plan", return_value=_three_window_sampling()),
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate", side_effect=score),
            ):
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")

            self.assertIn("coarse", seen_tiers)
            self.assertIn("exact", seen_tiers)
            self.assertEqual(result.status, QualitySearchStatus.FOUND)
            receipt = load_analysis_receipt(root, result.measurement_fingerprint)
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertTrue(receipt.candidates)
            self.assertTrue(all(len(candidate.segment_vmaf) == 3 for candidate in receipt.candidates))
            self.assertIn(result.selected_video_bitrate_bps, [candidate.video_bitrate_bps for candidate in result.candidates])
            selected = next(
                candidate
                for candidate in result.candidates
                if candidate.video_bitrate_bps == result.selected_video_bitrate_bps
            )
            self.assertGreaterEqual(selected.min_vmaf, item.options.min_vmaf)

    def test_scout_failure_is_terminal_without_fixed_window_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _analysis_item(root)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            failures = (
                ComplexityProbeError("missing lavfi.siti.si"),
                SamplePlanningError("independent holdouts unavailable"),
            )
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    with (
                        patch(
                            "core.smart.v1.workflow.select_vmaf_runtime",
                            return_value=VmafRuntimeSupport(
                                VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                            ),
                        ),
                        patch(
                            "core.smart.v1.workflow.detect_analysis_capabilities",
                            return_value=_capabilities(),
                        ),
                        patch(
                            "core.smart.v1.workflow.discover_sample_plan",
                            side_effect=failure,
                        ),
                        patch("core.smart.v1.session.score_candidate") as score,
                    ):
                        result = analyze_quality(
                            ffmpeg, item, root, root / "log.txt"
                        )
                    self.assertEqual(result.status, QualitySearchStatus.FAILED)
                    self.assertIn("content scout failed", result.reason or "")
                    score.assert_not_called()

    def test_failed_holdout_is_promoted_and_refined_upward(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _analysis_item(root)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            search_calls = 0
            reference_counts: list[int] = []

            def search(**kwargs):
                nonlocal search_calls
                search_calls += 1
                bitrate = 2_000_000 if search_calls >= 3 else 1_000_000
                result = kwargs["evaluate"](bitrate)
                return [result], bitrate, bitrate

            def score(_ffmpeg, _item, references, bitrate, *_args, **_kwargs):
                reference_counts.append(len(references))
                holdout_probe = len(references) == 1
                vmaf = 89.0 if holdout_probe and bitrate < 2_000_000 else 95.0
                return QualityCandidateResult(
                    video_bitrate_bps=bitrate,
                    min_vmaf=vmaf,
                    segment_vmaf=[vmaf] * len(references),
                    observed_video_bitrate_bps=bitrate,
                )

            progress: list[ProgressEvent] = []
            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(
                        VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                    ),
                ),
                patch("core.smart.v1.workflow.detect_analysis_capabilities", return_value=_capabilities()),
                patch("core.smart.v1.workflow.discover_sample_plan", return_value=_sampling_with_holdout()),
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate", side_effect=score),
                patch("core.smart.v1.search.search_bitrate_candidates", side_effect=search),
            ):
                result = analyze_quality(
                    ffmpeg,
                    item,
                    root,
                    root / "log.txt",
                    progress_callback=progress.append,
                )
            self.assertEqual(result.status, QualitySearchStatus.FOUND)
            self.assertEqual(result.selected_video_bitrate_bps, 2_000_000)
            self.assertTrue(any(event.get("state") == "refining" for event in progress))
            receipt = load_analysis_receipt(root, result.measurement_fingerprint)
            self.assertIsNotNone(receipt)
            assert receipt is not None
            self.assertEqual(len(receipt.search_windows), 5)
            self.assertEqual(len(receipt.holdout_windows), 1)
            holdout_reasons = receipt.holdout_windows[0].get("reasons")
            if not isinstance(holdout_reasons, list):
                self.fail("holdout window receipt has no reasons list")
            self.assertIn("fresh_reserve_holdout", holdout_reasons)
            self.assertTrue(receipt.independent_final_holdout)
            self.assertEqual(
                receipt.refinement_rounds[0]["promoted_window_ids"],
                ["holdout:test-1", "holdout:test-2"],
            )

            changed_policy = replace(
                item,
                options=replace(item.options, min_vmaf=94.0),
                quality_search_result=None,
            )
            reference_counts.clear()
            reused_progress: list[ProgressEvent] = []
            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(
                        VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                    ),
                ),
                patch(
                    "core.smart.v1.workflow.detect_analysis_capabilities",
                    return_value=_capabilities(),
                ),
                patch(
                    "core.smart.v1.workflow.discover_sample_plan",
                    side_effect=AssertionError("receipt windows should be reused"),
                ),
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate", side_effect=score),
                patch(
                    "core.smart.v1.search.search_bitrate_candidates",
                    side_effect=search,
                ),
            ):
                reused = analyze_quality(
                    ffmpeg,
                    changed_policy,
                    root,
                    root / "reuse.log",
                    progress_callback=reused_progress.append,
                )

            self.assertEqual(reused.status, QualitySearchStatus.FOUND)
            self.assertTrue(reference_counts)
            self.assertEqual(set(reference_counts), {1, 5})
            self.assertTrue(
                any(
                    event.get("state") == "holdout_verification"
                    for event in reused_progress
                )
            )

    def test_near_size_boundary_calibrates_without_vmaf_before_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _analysis_item(root)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")

            def search(**kwargs):
                result = kwargs["evaluate"](2_000_000)
                return [result], 2_000_000, 2_000_000

            def score(_ffmpeg, _item, references, bitrate, *_args, **_kwargs):
                count = len(references)
                prediction = SizePrediction(
                    mean_video_bitrate_bps=10_000_000,
                    upper_video_bitrate_bps=11_000_000,
                    predicted_output_bytes=82_000_000,
                    predicted_output_ratio=0.82,
                    uncertainty=0.10,
                    method="test_boundary",
                )
                return QualityCandidateResult(
                    video_bitrate_bps=bitrate,
                    min_vmaf=95.0,
                    segment_vmaf=[95.0] * count,
                    observed_window_bitrates=[2_000_000] * count,
                    observed_video_bitrate_bps=10_000_000,
                    predicted_output_bytes=82_000_000,
                    predicted_output_ratio=0.82,
                    size_prediction=prediction,
                )

            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True),
                ),
                patch("core.smart.v1.workflow.detect_analysis_capabilities", return_value=_capabilities()),
                patch("core.smart.v1.workflow.discover_sample_plan", return_value=_sampling_with_holdout()),
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate", side_effect=score),
                patch("core.smart.v1.search.search_bitrate_candidates", side_effect=search),
                patch(
                    "core.smart.v1.search.measure_size_only",
                    return_value=[1_000_000, 1_100_000],
                ) as calibrate,
            ):
                result = analyze_quality(ffmpeg, item, root, root / "size.log")

            calibrate.assert_called_once()
            self.assertEqual(result.status, QualitySearchStatus.FOUND)

    def test_holdout_failure_at_refinement_limit_requires_quality_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = _analysis_item(root, max_refinement_rounds=0)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")

            def score(_ffmpeg, _item, references, bitrate, *_args, **_kwargs):
                vmaf = 89.0 if len(references) == 1 else 95.0
                return QualityCandidateResult(
                    video_bitrate_bps=bitrate,
                    min_vmaf=vmaf,
                    segment_vmaf=[vmaf] * len(references),
                    observed_video_bitrate_bps=bitrate,
                )

            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(
                        VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                    ),
                ),
                patch("core.smart.v1.workflow.detect_analysis_capabilities", return_value=_capabilities()),
                patch("core.smart.v1.workflow.discover_sample_plan", return_value=_sampling_with_holdout()),
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate", side_effect=score),
            ):
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")
            self.assertEqual(result.status, QualitySearchStatus.CONSTRAINT_UNSATISFIED)
            self.assertEqual(result.failure_kind, ConstraintFailureKind.QUALITY_UNREACHABLE)
            self.assertNotIn(DecisionActionCode.RELAX_QUALITY,
                             [option.action_code for option in build_decision_options(result)])
            self.assertIn("refinement limit", result.reason or "")

    def test_loopback_failure_falls_back_to_legacy_scoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"x" * 100_000_000)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = EncodePlanItem(
                source_path=source,
                output_path=root / "out.mp4",
                media_info=MediaInfo(
                    path=source,
                    duration=12.0,
                    format_bitrate_bps=4_000_000,
                    video_bitrate_bps=3_000_000,
                    audio_bitrate_bps=128_000,
                    width=1920,
                    height=1080,
                    fps=29.97,
                    video_codec="h264",
                    audio_codec="aac",
                    audio_stream_count=1,
                    pix_fmt="yuv420p",
                    color_transfer="bt709",
                ),
                encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
                options=EncodeOptions(encoder_preset="slow"),
            )
            calls: list[str] = []

            def fake_loopback(*_args, **_kwargs):
                calls.append("loopback")
                raise RuntimeError("loopback encoder failed")

            def fake_score(*_args, **kwargs):
                calls.append("legacy")
                bitrate = _args[3]
                return QualityCandidateResult(
                    video_bitrate_bps=bitrate,
                    min_vmaf=96.0,
                    segment_vmaf=[96.0, 96.0, 96.0],
                    observed_video_bitrate_bps=bitrate,
                )

            loopback_caps = _capabilities(loopback_decoder=True)
            with (
                patch(
                    "core.smart.v1.workflow.select_vmaf_runtime",
                    return_value=VmafRuntimeSupport(
                        VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True
                    ),
                ),
                patch("core.smart.v1.workflow.detect_analysis_capabilities", return_value=loopback_caps),
                patch("core.smart.v1.workflow.discover_sample_plan", return_value=_three_window_sampling()),
                patch("core.smart.v1.workflow.build_analysis_execution_plan") as planner,
                patch("core.smart.v1.workflow._run_logged"),
                patch("core.smart.v1.session.run_logged"),
                patch("core.smart.v1.session.score_candidate_loopback", side_effect=fake_loopback),
                patch("core.smart.v1.session.score_candidate", side_effect=fake_score),
            ):
                def plan_for(*, tier, **kwargs):
                    assert item.encoder_info is not None
                    return build_analysis_execution_plan(
                        tier=tier,
                        encoder_info=item.encoder_info,
                        production_preset="slow",
                        production_two_pass=True,
                        capabilities=loopback_caps,
                        enable_loopback=True,
                    )

                planner.side_effect = plan_for
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")

            self.assertIn("loopback", calls)
            self.assertIn("legacy", calls)
            self.assertEqual(result.status, QualitySearchStatus.FOUND)


if __name__ == "__main__":
    unittest.main(verbosity=2)
