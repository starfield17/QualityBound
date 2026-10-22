from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.smart.v1.runtime import (
    SOURCE_DECODE_SOFTWARE,
    SOURCE_DECODE_VIDEOTOOLBOX,
    AnalysisTier,
    VmafBackend,
    build_analysis_execution_plan,
    cpu_vmaf_plan,
)
from core.models import (
    OperationCancelledError,
    QualityCandidateResult,
    QualitySearchStatus,
    VmafRuntimeSupport,
)
from core.smart.v1.cache import measurement_configuration_fingerprint
from core.smart.v1.workflow import analyze_quality
from core.smart.v1.measurement import SmartCommandError
from core.smart.v1.sampling.planner import PlannedWindow
from core.smart.v1.vmaf import EXACT_VMAF_SUBSAMPLE

from _analysis_runtime_fixtures import (
    _session,
    _analysis_item,
    _capabilities,
    _sampling_with_holdout,
    _three_window_sampling,
)


class AnalysisSessionTestCase(unittest.TestCase):
    def test_subset_reference_does_not_overwrite_search_and_expansion_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _session(Path(directory))
            extracted: list[Path] = []

            def extract(command, *_args, **_kwargs):
                path = Path(command[-1])
                path.write_text(command[command.index("-ss") + 1])
                extracted.append(path)

            with patch("core.smart.v1.session.run_logged", side_effect=extract):
                session.ensure_references(session.exact_plan)
                originals = {path: path.read_text() for path in session.references}
                subset = [PlannedWindow("holdout:new", 73.0, 5.0, ("validation",))]
                with patch("core.smart.v1.session.score_candidate", return_value=QualityCandidateResult(
                    video_bitrate_bps=900_000, min_vmaf=95.0, segment_vmaf=[95.0],
                )):
                    session.evaluate_planned_subset(900_000, subset)
                self.assertEqual({path: path.read_text() for path in originals}, originals)
                self.assertEqual(len(set(extracted)), 4)
                session.planned_search.extend(subset)
                session.reset_search_windows()
                session.ensure_references(session.exact_plan)
                self.assertEqual(len(extracted), 4)
                self.assertEqual(session.references[-1].read_text(), "73.000")


    def test_measurement_resources_and_counters_are_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = _session(Path(directory))
            second = _session(Path(directory))
            first.references.append(Path("first-reference.mkv"))
            first.candidate_indexes[AnalysisTier.EXACT] += 1
            first.planned_search.pop()
            first.reset_search_windows()
            self.assertEqual(second.references, [])
            self.assertEqual(second.candidate_indexes[AnalysisTier.EXACT], 0)
            self.assertEqual(len(second.windows), 3)
            self.assertEqual(len(second.planned_search), 3)
            self.assertEqual(len(first.windows), 2)

    def test_subset_restores_search_state_on_success_and_cancellation(self) -> None:
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled), tempfile.TemporaryDirectory() as directory:
                session = _session(Path(directory))
                session.references = [Path("search-reference.mkv")]
                session.hardest_window = 2
                windows, references = session.windows, session.references
                holdout = _sampling_with_holdout().plan.holdout_windows[0]
                expected = QualityCandidateResult(video_bitrate_bps=900_000, min_vmaf=97, segment_vmaf=[97])

                def evaluate(
                    _bitrate,
                    _plan,
                    *,
                    session=session,
                    holdout=holdout,
                    cancelled=cancelled,
                    expected=expected,
                ):
                    self.assertEqual(len(session.windows), 1)
                    self.assertEqual(session.windows[0].start_sec, holdout.start_sec)
                    self.assertEqual(session.references, [])
                    self.assertEqual(session.hardest_window, 0)
                    session.exact_plan = cpu_vmaf_plan(session.exact_plan, reason="test fallback")
                    if cancelled:
                        raise OperationCancelledError("cancelled")
                    return expected

                with patch.object(session, "evaluate", side_effect=evaluate):
                    if cancelled:
                        with self.assertRaises(OperationCancelledError):
                            session.evaluate_planned_subset(900_000, [holdout])
                    else:
                        self.assertIs(session.evaluate_planned_subset(900_000, [holdout]), expected)
                self.assertIs(session.windows, windows)
                self.assertIs(session.references, references)
                self.assertEqual(session.hardest_window, 2)
                self.assertEqual(session.exact_plan.fallback_reason, "test fallback")

    def test_decode_fallback_updates_both_tiers_and_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _session(Path(directory))
            session.exact_plan = replace(session.exact_plan, source_decode_acceleration=SOURCE_DECODE_VIDEOTOOLBOX)
            session.coarse_plan = replace(session.coarse_plan, source_decode_acceleration=SOURCE_DECODE_VIDEOTOOLBOX)
            failure = SmartCommandError(1, ["ffmpeg"], "reference extraction", "hardware unavailable")
            with patch("core.smart.v1.session.run_logged", side_effect=[failure, "", "", ""]) as run:
                active = session.ensure_references(session.coarse_plan)
                self.assertEqual(run.call_count, 4)
            self.assertEqual(active.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
            self.assertEqual(session.exact_plan.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
            self.assertEqual(session.coarse_plan.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
            with patch("core.smart.v1.session.run_logged") as run:
                session.ensure_references(session.exact_plan)
                run.assert_not_called()

    def test_cuda_fallback_persists_for_subsequent_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = _session(Path(directory))
            session.references = [Path(f"reference-{index}.mkv") for index in range(3)]
            session.exact_plan = replace(session.exact_plan, vmaf_backend=VmafBackend.CUDA)
            session.coarse_plan = replace(session.coarse_plan, vmaf_backend=VmafBackend.CUDA)
            failure = SmartCommandError(1, ["ffmpeg"], "VMAF", "CUDA unavailable")
            candidate = QualityCandidateResult(video_bitrate_bps=900_000, min_vmaf=97, segment_vmaf=[97]*3)
            with patch("core.smart.v1.session.score_candidate", side_effect=[failure, candidate, candidate]) as score:
                session.evaluate(900_000, session.coarse_plan)
                session.evaluate(900_000, session.exact_plan)
            self.assertEqual(
                [call.kwargs["plan"].vmaf_backend for call in score.call_args_list],
                [VmafBackend.CUDA, VmafBackend.CPU, VmafBackend.CPU],
            )
            self.assertEqual(session.exact_plan.vmaf_backend, VmafBackend.CPU)
            self.assertEqual(session.coarse_plan.vmaf_backend, VmafBackend.CPU)

    def test_workflow_persists_actual_fallback_identity_and_reports_write_failure(self) -> None:
        for write_fails in (False, True):
            with self.subTest(write_fails=write_fails), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                item = _analysis_item(root)
                ffmpeg = root / "ffmpeg"
                ffmpeg.write_bytes(b"binary")
                events = []
                backends = []

                def plan_for(**kwargs):
                    return replace(build_analysis_execution_plan(**kwargs), vmaf_backend=VmafBackend.CUDA)

                def score(*args, backends=backends, **kwargs):
                    backends.append(kwargs["plan"].vmaf_backend)
                    if len(backends) == 1:
                        raise SmartCommandError(1, ["ffmpeg"], "VMAF", "CUDA unavailable")
                    return QualityCandidateResult(
                        video_bitrate_bps=args[3], min_vmaf=99, segment_vmaf=[99]*3,
                        observed_video_bitrate_bps=args[3],
                    )

                with (
                    patch("core.smart.v1.workflow.select_vmaf_runtime", return_value=VmafRuntimeSupport(VmafBackend.CUDA, "vmaf_v1.0.16_3d0h", True)),
                    patch("core.smart.v1.workflow.detect_analysis_capabilities", return_value=_capabilities()),
                    patch("core.smart.v1.workflow.build_analysis_execution_plan", side_effect=plan_for),
                    patch("core.smart.v1.workflow.discover_sample_plan", return_value=_three_window_sampling()),
                    patch("core.smart.v1.session.run_logged"),
                    patch("core.smart.v1.session.score_candidate", side_effect=score),
                    patch("core.smart.v1.workflow.save_analysis_receipt", side_effect=OSError("disk full") if write_fails else None) as save,
                ):
                    result = analyze_quality(ffmpeg, item, root, root / "analysis.log", progress_callback=events.append)

                self.assertEqual(result.status, QualitySearchStatus.FOUND)
                self.assertEqual(backends[0], VmafBackend.CUDA)
                self.assertTrue(all(backend == VmafBackend.CPU for backend in backends[1:]))
                save.assert_called_once()
                receipt = save.call_args.args[1]
                self.assertEqual(receipt.measurement_fingerprint, result.measurement_fingerprint)
                self.assertEqual(result.measurement_fingerprint, measurement_configuration_fingerprint(
                    ffmpeg, item, vmaf_backend=VmafBackend.CPU, vmaf_subsample=EXACT_VMAF_SUBSAMPLE,
                ))
                failures = [event for event in events if event.get("state") == "receipt_write_failed"]
                self.assertEqual(len(failures), int(write_fails))
                if write_fails:
                    self.assertEqual(failures[0]["message"], "disk full")

    def test_workflow_cancellation_cleans_temporary_resources_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = _analysis_item(root)
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            with (
                patch("core.smart.v1.workflow.select_vmaf_runtime", return_value=VmafRuntimeSupport(VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True)),
                patch("core.smart.v1.workflow.detect_analysis_capabilities", return_value=_capabilities()),
                patch("core.smart.v1.workflow.discover_sample_plan", return_value=_three_window_sampling()),
                patch("core.smart.v1.session.run_logged", side_effect=OperationCancelledError("cancelled")),
                patch("core.smart.v1.workflow.save_analysis_receipt") as save,
            ):
                with self.assertRaises(OperationCancelledError):
                    analyze_quality(ffmpeg, item, root, root / "analysis.log")
            save.assert_not_called()
            self.assertEqual(list(root.glob("smart-analysis-*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
