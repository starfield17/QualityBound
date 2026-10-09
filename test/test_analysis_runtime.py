from __future__ import annotations

import json
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.smart.v1.receipts import ANALYSIS_RECEIPT_SCHEMA_VERSION, load_analysis_receipt
from core.smart.v1.runtime import (
    COARSE_MAX_CANDIDATES,
    EXACT_MAX_CANDIDATES,
    SOURCE_DECODE_SOFTWARE,
    SOURCE_DECODE_VIDEOTOOLBOX,
    AnalysisCapabilities,
    AnalysisDecodePolicy,
    AnalysisTier,
    VmafBackend,
    build_analysis_execution_plan,
    coarse_encoder_preset,
    cpu_vmaf_plan,
    format_analysis_capability_report,
    help_exposes_loopback_decoder,
    legacy_loopback_plan,
    parse_decoder_names,
    parse_filter_names,
    search_tolerance_bps,
    software_source_plan,
    source_decode_args,
)
from core.ffmpeg.commands import build_video_args
from core.models import (
    AnalysisReceipt,
    BackendChoice,
    CodecChoice,
    ConstraintFailureKind,
    DecisionActionCode,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
    MediaInfo,
    OperationCancelledError,
    QualityCandidateResult,
    QualitySearchStatus,
    SizePrediction,
    VmafRuntimeSupport,
    VmafViewingContext,
)
from core.smart.v1.cache import SMART_ANALYSIS_ALGORITHM_VERSION, SMART_SAMPLE_SCHEME_VERSION, measurement_configuration_fingerprint
from core.smart.v1.concurrency import SMART_ANALYSIS_SEMAPHORE
from core.smart.v1.workflow import analyze_quality
from core.smart.v1.decisions import build_decision_options
from core.smart.v1.bitrate import calculate_smart_bitrate_budget, search_bitrate_candidates
from core.smart.v1.measurement import SampleWindow, SmartCommandError
from core.progress_events import ProgressEvent
from core.smart.v1.session import AnalysisSession
from core.smart.v1.measurement import build_loopback_score_command as _build_loopback_score_command
from core.smart.v1.measurement import build_reference as _build_reference
from core.smart.v1.measurement import score_candidate as _score_candidate
from core.smart.v1.sampling.planner import PlannedWindow, SamplePlan, SamplePlanningError
from core.smart.v1.sampling.scout import SamplingResult
from core.smart.v1.sampling.complexity import ComplexityProbeError
from core.smart.v1.vmaf import (
    COARSE_VMAF_SUBSAMPLE,
    EXACT_VMAF_SUBSAMPLE,
    VMAF_STANDARD_MODEL,
    VmafEncodeMetadata,
)


def _capabilities(**overrides: object) -> AnalysisCapabilities:
    payload: dict[str, object] = dict(
        libvmaf=True,
        libvmaf_cuda=False,
        loopback_decoder=False,
        hwaccels=frozenset({"videotoolbox"}),
        filters=frozenset({"libvmaf", "scale_vt"}),
        encoders=frozenset({"libx265", "libsvtav1", "hevc_videotoolbox"}),
        scale_vt=True,
        scale_cuda=False,
        videotoolbox_hwaccel=True,
        cuda_hwaccel=False,
        videotoolbox_prio_speed=True,
    )
    payload.update(overrides)
    return AnalysisCapabilities(**payload)  # type: ignore[arg-type]


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


def _sampling_with_holdout() -> SamplingResult:
    base = _three_window_sampling().plan
    holdouts = (
        PlannedWindow(
            id=f"holdout:test-{index}",
            start_sec=start,
            duration_sec=5.0,
            reasons=("test_holdout",),
            scout_id=f"scout-holdout-{index}",
        )
        for index, start in enumerate((50.0, 55.0), 1)
    )
    reserve = PlannedWindow(
        id="reserve:test",
        start_sec=45.0,
        duration_sec=5.0,
        reasons=("test_reserve",),
        scout_id="scout-reserve",
    )
    return SamplingResult(
        SamplePlan(
            base.scout_windows,
            base.search_windows,
            tuple(holdouts),
            False,
            reserve_windows=(reserve,),
        ),
        (),
    )


def _encoder(name: str, backend: BackendChoice, *, two_pass: bool = False, preset: str | None = None) -> EncoderInfo:
    return EncoderInfo(
        codec=CodecChoice.AV1 if "av1" in name or name == "libsvtav1" else CodecChoice.HEVC,
        backend=backend,
        encoder_name=name,
        supports_two_pass=two_pass,
        default_preset=preset,
    )


def _analysis_item(root: Path, *, max_refinement_rounds: int = 2) -> EncodePlanItem:
    source = root / "source.mov"
    with source.open("wb") as fh:
        fh.truncate(100_000_000)
    options = EncodeOptions(
        encoder_preset="slow",
        max_output_ratio=0.8,
        analysis_settings=replace(
            EncodeOptions().analysis_settings,
            preferred_vmaf_margin=0.0,
            max_refinement_rounds=max_refinement_rounds,
        ),
    )
    return EncodePlanItem(
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
        options=options,
    )


class AnalysisSessionTestCase(unittest.TestCase):
    def test_subset_reference_does_not_overwrite_search_and_expansion_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = self._session(Path(directory))
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

    def _session(self, root: Path) -> AnalysisSession:
        item = _analysis_item(root)
        assert item.encoder_info is not None
        plans = {
            tier: build_analysis_execution_plan(
                tier=tier,
                encoder_info=item.encoder_info,
                production_preset=item.options.encoder_preset,
                production_two_pass=item.options.two_pass,
                capabilities=_capabilities(),
            )
            for tier in AnalysisTier
        }
        return AnalysisSession(
            ffmpeg_path=root / "ffmpeg", item=item, workdir=root,
            temp_root=root, log_file=io.StringIO(),
            budget=calculate_smart_bitrate_budget(item),
            exact_plan=plans[AnalysisTier.EXACT], coarse_plan=plans[AnalysisTier.COARSE],
            planned_search=list(_three_window_sampling().plan.search_windows),
            scout_observations=[], initial_candidates=[],
        )

    def test_measurement_resources_and_counters_are_per_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = self._session(Path(directory))
            second = self._session(Path(directory))
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
                session = self._session(Path(directory))
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
            session = self._session(Path(directory))
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
            session = self._session(Path(directory))
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


class AnalysisCapabilityParsingTestCase(unittest.TestCase):
    def test_filter_and_decoder_parsers(self) -> None:
        filters = parse_filter_names(
            "Filters:\n"
            " .. libvmaf           VV->V      Calculate the VMAF\n"
            " TSC scale_vt         V->V       Scale Videotoolbox frames\n"
            " .. libvmaf_cuda      VV->V      CUDA VMAF\n"
        )
        self.assertEqual(filters, frozenset({"libvmaf", "scale_vt", "libvmaf_cuda"}))
        decoders = parse_decoder_names(" V..... loopback            Loopback decoder\n V....D hevc                HEVC")
        self.assertIn("loopback", decoders)

    def test_loopback_help_detection_and_legacy_fallback(self) -> None:
        self.assertTrue(help_exposes_loopback_decoder("  -dec[:stream] decoder   loopback decoder"))
        self.assertFalse(help_exposes_loopback_decoder("  -decode                  unused"))
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
            production_preset="slow",
            production_two_pass=True,
            capabilities=_capabilities(loopback_decoder=True),
            enable_loopback=True,
        )
        self.assertTrue(plan.use_loopback)
        fallback = legacy_loopback_plan(plan, reason="loopback failed")
        self.assertFalse(fallback.use_loopback)
        self.assertEqual(fallback.fallback_reason, "loopback failed")

    def test_capability_report_does_not_require_optional_features(self) -> None:
        report = format_analysis_capability_report(_capabilities(libvmaf_cuda=False, loopback_decoder=False))
        self.assertIn("CPU VMAF", report)
        self.assertIn("CUDA VMAF filter (not enabled for v1)", report)
        self.assertIn("Loopback decoder", report)
        self.assertIn("VideoToolbox", report)


class AnalysisExecutionPlanTestCase(unittest.TestCase):
    def test_coarse_uses_faster_preset_and_one_pass(self) -> None:
        coarse = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
            production_preset="slow",
            production_two_pass=True,
            capabilities=_capabilities(),
        )
        exact = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
            production_preset="slow",
            production_two_pass=True,
            capabilities=_capabilities(),
        )
        self.assertEqual(coarse.encoder_name, "libx265")
        self.assertEqual(coarse.encoder_preset, "fast")
        self.assertFalse(coarse.two_pass)
        self.assertEqual(coarse.vmaf_subsample, COARSE_VMAF_SUBSAMPLE)
        self.assertEqual(exact.encoder_preset, "slow")
        self.assertTrue(exact.two_pass)
        self.assertEqual(exact.vmaf_subsample, EXACT_VMAF_SUBSAMPLE)
        self.assertGreaterEqual(exact.vmaf_threads, 1)

    def test_svt_and_nvenc_coarse_presets_stay_on_the_same_encoder(self) -> None:
        self.assertEqual(coarse_encoder_preset("libsvtav1", "5"), "8")
        self.assertEqual(coarse_encoder_preset("hevc_nvenc", "p6"), "p4")
        svt = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=_encoder("libsvtav1", BackendChoice.CPU, preset="5"),
            production_preset="5",
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        self.assertEqual(svt.encoder_name, "libsvtav1")
        self.assertEqual(svt.encoder_preset, "8")

    def test_videotoolbox_coarse_adds_prio_speed_exact_does_not(self) -> None:
        encoder = _encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX)
        coarse = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=encoder,
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        exact = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=encoder,
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        self.assertEqual(coarse.encoder_extra_args, ("-prio_speed", "1"))
        self.assertEqual(exact.encoder_extra_args, ())
        self.assertEqual(coarse.source_decode_acceleration, SOURCE_DECODE_VIDEOTOOLBOX)
        item = EncodePlanItem(
            source_path=Path("in.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=encoder,
            options=EncodeOptions(codec=CodecChoice.HEVC, backend=BackendChoice.VIDEOTOOLBOX),
            target_video_bitrate_bps=1_000_000,
        )
        exact_args = build_video_args(item, extra_args=exact.encoder_extra_args)
        coarse_args = build_video_args(item, extra_args=coarse.encoder_extra_args)
        self.assertEqual(exact_args[exact_args.index("-allow_sw") + 1], "0")
        self.assertNotIn("-prio_speed", exact_args)
        self.assertEqual(coarse_args[coarse_args.index("-allow_sw") + 1], "0")
        self.assertEqual(coarse_args[coarse_args.index("-prio_speed") + 1], "1")

    def test_software_policy_never_selects_videotoolbox_decode(self) -> None:
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
            decode_policy=AnalysisDecodePolicy.SOFTWARE,
        )
        self.assertEqual(plan.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
        self.assertEqual(source_decode_args(SOURCE_DECODE_SOFTWARE), [])
        self.assertEqual(source_decode_args(SOURCE_DECODE_VIDEOTOOLBOX), ["-hwaccel", "videotoolbox"])

    def test_av1_never_binds_videotoolbox_encoder_but_may_decode_with_vt(self) -> None:
        with self.assertRaises(ValueError):
            build_analysis_execution_plan(
                tier=AnalysisTier.EXACT,
                encoder_info=_encoder("av1_videotoolbox", BackendChoice.VIDEOTOOLBOX),
                production_preset="5",
                production_two_pass=False,
                capabilities=_capabilities(),
            )
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=_encoder("libsvtav1", BackendChoice.CPU, preset="5"),
            production_preset="5",
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        self.assertEqual(plan.encoder_name, "libsvtav1")
        self.assertEqual(plan.source_decode_acceleration, SOURCE_DECODE_VIDEOTOOLBOX)

    def test_static_cuda_presence_does_not_enable_v1_cuda(self) -> None:
        cuda = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("hevc_nvenc", BackendChoice.NVENC, preset="p6"),
            production_preset="p6",
            production_two_pass=False,
            capabilities=_capabilities(
                libvmaf_cuda=True,
                scale_cuda=True,
                cuda_hwaccel=True,
                hwaccels=frozenset({"cuda"}),
                videotoolbox_hwaccel=False,
            ),
        )
        self.assertEqual(cuda.vmaf_backend, VmafBackend.CPU)
        fallback = cpu_vmaf_plan(cuda, reason="cuda vmaf failed")
        self.assertEqual(fallback.vmaf_backend, VmafBackend.CPU)
        self.assertFalse(fallback.use_loopback)

    def test_source_decode_fallback_drops_hardware(self) -> None:
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        fallback = software_source_plan(plan, reason="vt failed")
        self.assertEqual(fallback.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
        self.assertEqual(fallback.encoder_name, "hevc_videotoolbox")


class SmartAnalyseV2TestCase(unittest.TestCase):
    def test_ffv1_reference_never_gets_videotoolbox_by_default_helper_can_add_it(self) -> None:
        item = EncodePlanItem(
            source_path=Path("movie.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            options=EncodeOptions(),
        )
        software = _build_reference(Path("ffmpeg"), item, SampleWindow(1.0, 5.0), Path("ref.mkv"))
        hardware = _build_reference(
            Path("ffmpeg"),
            item,
            SampleWindow(1.0, 5.0),
            Path("ref.mkv"),
            decode_acceleration=SOURCE_DECODE_VIDEOTOOLBOX,
        )
        self.assertNotIn("-hwaccel", software)
        self.assertEqual(hardware[hardware.index("-hwaccel") + 1], "videotoolbox")
        self.assertLess(hardware.index("-hwaccel"), hardware.index("-i"))
        self.assertIn("ffv1", software)
        self.assertIn("settb=AVTB,setpts=PTS-STARTPTS", software)

    def test_loopback_command_uses_dec_and_keeps_legacy_builder(self) -> None:
        encoder = _encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX)
        item = EncodePlanItem(
            source_path=Path("movie.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=encoder,
            options=EncodeOptions(),
            target_video_bitrate_bps=1_000_000,
        )
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=encoder,
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(loopback_decoder=True),
            enable_loopback=True,
        )
        command = _build_loopback_score_command(
            Path("ffmpeg"),
            item,
            SampleWindow(0.0, 5.0),
            Path("cand.mkv"),
            plan,
            model_spec=VMAF_STANDARD_MODEL,
            encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
            log_name="vmaf.json",
        )
        self.assertEqual(command[command.index("-loglevel") + 1], "error")
        self.assertIn("-dec:v", command)
        self.assertIn("hevc", command)
        self.assertIn("[dec:v]", command[command.index("-filter_complex") + 1])

    def test_selected_bitrate_cannot_come_from_coarse_candidates(self) -> None:
        coarse_only: list[int] = []
        exact_only: list[int] = []

        def coarse(bitrate: int) -> QualityCandidateResult:
            coarse_only.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=96.0, predicted_output_bytes=100)

        def exact(bitrate: int) -> QualityCandidateResult:
            exact_only.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=96.0, predicted_output_bytes=100)

        coarse_candidates, coarse_selected, _ = search_bitrate_candidates(
            evaluate=coarse,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=2_000_000,
            required_search_ceiling_bps=3_000_000,
            min_vmaf=95.0,
            max_candidates=COARSE_MAX_CANDIDATES,
            max_output_bytes=1_000,
            tolerance_bps=search_tolerance_bps(3_000_000),
        )
        exact_candidates, exact_selected, _ = search_bitrate_candidates(
            evaluate=exact,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=coarse_selected or 2_000_000,
            required_search_ceiling_bps=3_000_000,
            min_vmaf=95.0,
            max_candidates=EXACT_MAX_CANDIDATES,
            max_output_bytes=1_000,
            tolerance_bps=search_tolerance_bps(3_000_000),
        )
        self.assertTrue(coarse_candidates)
        self.assertTrue(exact_candidates)
        self.assertIsNotNone(exact_selected)
        self.assertIn(exact_selected, exact_only)
        self.assertLessEqual(len(coarse_only), 4)
        self.assertLessEqual(len(exact_only), 3)

    def test_old_receipt_schema_is_ignored_and_backend_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = AnalysisReceipt(
                schema_version=1,
                measurement_fingerprint="a" * 64,
                source_identity={"path": "s"},
                ffmpeg_identity={"path": "f"},
                encoder_identity={"encoder": "libx265"},
                sample_scheme_version=1,
                sample_windows=[(0.0, 10.0)],
                candidates=[QualityCandidateResult(video_bitrate_bps=1_000_000, min_vmaf=96.0)],
            )
            path = root / "analysis" / "receipts" / f"{'a' * 64}.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "measurement_fingerprint": old.measurement_fingerprint,
                        "source_identity": old.source_identity,
                        "ffmpeg_identity": old.ffmpeg_identity,
                        "encoder_identity": old.encoder_identity,
                        "sample_scheme_version": 1,
                        "sample_windows": old.sample_windows,
                        "candidates": [],
                        "created_at": "",
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(load_analysis_receipt(root, "a" * 64))

            source = root / "source.mov"
            source.write_bytes(b"source")
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
                    pix_fmt="yuv420p",
                    bit_depth=8,
                ),
                encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
                options=EncodeOptions(encoder_preset="slow"),
            )
            cpu = measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU)
            cuda = measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CUDA)
            self.assertNotEqual(cpu, cuda)
            standard_display = replace(
                item,
                options=replace(
                    item.options,
                    viewing_context=VmafViewingContext.STANDARD_DISPLAY,
                ),
            )
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, standard_display),
                cpu,
            )
            self.assertEqual(SMART_SAMPLE_SCHEME_VERSION, 7)
            self.assertEqual(SMART_ANALYSIS_ALGORITHM_VERSION, 9)
            self.assertEqual(ANALYSIS_RECEIPT_SCHEMA_VERSION, 5)
            from core.smart.v1.cache import measurement_configuration_payload

            payload = measurement_configuration_payload(ffmpeg, item, vmaf_backend=VmafBackend.CPU)
            self.assertEqual(payload["sample_scheme_version"], 7)
            self.assertEqual(payload["vmaf_subsample"], 1)
            self.assertEqual(payload["vmaf_backend"], "cpu")
            self.assertEqual(payload["analysis_algorithm_version"], 9)
            self.assertEqual(payload["vmaf_resolution_mode"], "display_model_canvas")
            self.assertEqual(payload["vmaf_generation"], "v1")
            self.assertEqual(payload["vmaf_model"], "vmaf_v1.0.16_3d0h")
            self.assertEqual(payload["vmaf_measurement_pix_fmt"], "yuv420p10le")
            self.assertEqual(payload["vmaf_measurement_bit_depth"], 10)
            self.assertEqual(payload["vmaf_measurement_pipeline_version"], 3)
            self.assertEqual(payload["vmaf_scale_algorithm"], "bicubic")
            self.assertEqual(
                payload["vmaf_aspect_policy"], "square_pixels_fit_and_even_pad"
            )
            self.assertEqual(payload["candidate_encode_bit_depth"], 8)
            self.assertEqual(payload["vmaf_pooling"], "smart_v2_temporal_mean_worst_1s_v1")
            self.assertNotIn("n_threads", payload)
            self.assertNotIn("vmaf_threads", payload)
            assert item.media_info is not None
            item.media_info.fps = 60.0
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                cpu,
            )
            item.media_info.fps = 30.0
            item.options.pix_fmt = "p010le"
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                cpu,
            )
            item.options.pix_fmt = "yuv420p"
            item.media_info.bit_depth = 10
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                cpu,
            )
            item.media_info.bit_depth = 8
            with patch("core.smart.v1.cache.SMART_SAMPLE_SCHEME_VERSION", 99):
                self.assertNotEqual(
                    measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                    cpu,
                )

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

    def test_videotoolbox_reference_failure_retries_software(self) -> None:
        item = EncodePlanItem(
            source_path=Path("movie.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            options=EncodeOptions(),
        )
        from core.smart.v1.measurement import SampleWindow, SmartCommandError

        command = _build_reference(
            Path("ffmpeg"),
            item,
            SampleWindow(0.0, 5.0),
            Path("ref.mkv"),
            decode_acceleration=SOURCE_DECODE_VIDEOTOOLBOX,
        )
        self.assertEqual(command[command.index("-hwaccel") + 1], "videotoolbox")
        assert item.encoder_info is not None
        fallback = software_source_plan(
            build_analysis_execution_plan(
                tier=AnalysisTier.EXACT,
                encoder_info=item.encoder_info,
                production_preset=None,
                production_two_pass=False,
                capabilities=_capabilities(),
            ),
            reason="VideoToolbox operation failed",
        )
        self.assertEqual(fallback.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
        self.assertIsInstance(SmartCommandError(1, ["ffmpeg"], "reference extraction", "vt fail"), SmartCommandError)

    def test_concurrency_limit_stays_resource_bounded(self) -> None:
        from core.smart.v1.concurrency import analysis_concurrency_limit

        self.assertEqual(analysis_concurrency_limit(cpu_count=4), 1)
        self.assertEqual(analysis_concurrency_limit(cpu_count=8), 2)
        self.assertLessEqual(SMART_ANALYSIS_SEMAPHORE._value, 2)
        self.assertGreaterEqual(SMART_ANALYSIS_SEMAPHORE._value, 1)

    def test_score_candidate_early_rejects_remaining_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 100_000)
            item = EncodePlanItem(
                source_path=source,
                output_path=root / "out.mp4",
                media_info=None,
                encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
                options=EncodeOptions(encoder_preset="slow", min_vmaf=95.0),
            )
            from core.models import MediaInfo

            item.media_info = MediaInfo(
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
            )
            references = [root / "r0.mkv", root / "r1.mkv", root / "r2.mkv"]
            for path in references:
                path.write_bytes(b"ref")
            scored: list[int] = []

            def fake_run(command, _log, **kwargs):
                phase = kwargs.get("phase")
                if phase == "candidate encode":
                    Path(command[-1]).write_bytes(b"c" * 1_000)
                elif phase == "VMAF scoring":
                    scored.append(1)
                    graph = command[command.index("-filter_complex") + 1]
                    name = graph.split("log_path='")[1].split("'")[0]
                    (kwargs["cwd"] / name).write_text(
                        json.dumps(
                            {
                                "pooled_metrics": {"vmaf": {"mean": 90.0}},
                                "frames": [
                                    {"frameNum": index, "metrics": {"vmaf": 90.0}}
                                    for index in range(150)
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )

            log_path = root / "smart.log"
            with log_path.open("w", encoding="utf-8") as log_file, patch(
                "core.smart.v1.measurement.run_logged", side_effect=fake_run
            ):
                result = _score_candidate(
                    Path("ffmpeg"),
                    item,
                    references,
                    900_000,
                    root,
                    root,
                    log_file,
                    cancel_check=None,
                    process_callback=None,
                    window_durations_sec=[5.0, 5.0, 5.0],
                    min_vmaf_target=95.0,
                    window_order=[1, 0, 2],
                )
            self.assertEqual(len(scored), 1)
            self.assertEqual(result.segment_vmaf, [90.0])
            self.assertLess(result.min_vmaf, 95.0)


if __name__ == "__main__":
    unittest.main()
