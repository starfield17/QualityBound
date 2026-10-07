"""Smart-analysis lifecycle orchestration.

This module coordinates sampling, measurement, cache reuse, holdout validation,
and refinement. Bitrate policy, receipt identity, and FFmpeg/VMAF mechanics
live in their focused sibling modules.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, TextIO

from .session import AnalysisSession, emit_analysis_progress, sample_window
from .search import complete_candidates, run_search
from .receipts import load_analysis_receipt, save_analysis_receipt
from .runtime import (
    AnalysisDecodePolicy,
    AnalysisExecutionPlan,
    AnalysisTier,
    build_analysis_execution_plan,
    detect_analysis_capabilities,
)
from core.models import (
    AnalysisProfileSettings,
    AnalysisReceipt,
    CompressionMode,
    EncodePlanItem,
    OperationCancelledError,
    QualityCandidateResult,
    QualitySearchResult,
    QualitySearchStatus,
    VmafBackend,
    VmafRuntimeSupport,
)
from .sampling.complexity import ComplexityProbeError
from .sampling.planner import (
    SamplePlan,
    SamplePlanningError,
    ScoutObservation,
    planned_window_from_payload,
    scout_observation_from_payload,
    search_window_count,
    should_analyze_whole_video,
)
from core.progress_events import ProgressCallback
from .bitrate import (
    calculate_smart_bitrate_budget,
    refresh_candidate_predictions as _refresh_candidate_predictions,
    reselect_from_candidates,
)
from .cache import (
    SMART_SAMPLE_SCHEME_VERSION,
    analysis_receipt as _analysis_receipt,
    measurement_configuration_fingerprint,
    quality_configuration_fingerprint,
)
from .measurement import (
    SampleWindow,
    SmartCommandError,
    log_timing as _log_timing,
    run_logged as _run_logged,
)
from .sampling.scout import discover_sample_plan
from .vmaf import (
    VMAF_MEASUREMENT_BIT_DEPTH,
    VMAF_MEASUREMENT_PIX_FMT,
    candidate_encode_metadata,
    select_vmaf_model,
    select_vmaf_runtime,
)


HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def choose_smart_sample_windows(
    duration_sec: float,
    settings: AnalysisProfileSettings | None = None,
) -> list[SampleWindow]:
    """Return deterministic legacy windows when a caller does not use Scout."""
    if duration_sec <= 0:
        raise ValueError("Source duration must be greater than 0.")
    profile = settings or AnalysisProfileSettings()
    if should_analyze_whole_video(duration_sec, profile):
        return [SampleWindow(0.0, duration_sec)]
    window_count = search_window_count(duration_sec, profile)
    sample_duration = min(profile.sample_duration_sec, duration_sec / window_count)
    max_start = max(0.0, duration_sec - sample_duration)
    if window_count == 1:
        return [SampleWindow(max_start / 2.0, sample_duration)]
    fractions = tuple((index + 1) / (window_count + 1) for index in range(window_count))
    starts = [
        max(0.0, min(max_start, duration_sec * fraction - sample_duration / 2.0))
        for fraction in fractions
    ]
    if any(starts[index + 1] < starts[index] + sample_duration for index in range(len(starts) - 1)):
        starts = [max_start * index / max(window_count - 1, 1) for index in range(window_count)]
    return [SampleWindow(start, sample_duration) for start in starts]


def _unsupported_reason(item: EncodePlanItem, support: VmafRuntimeSupport | None = None) -> str | None:
    media = item.media_info
    if media and media.color_transfer and media.color_transfer.lower() in HDR_TRANSFERS:
        return f"HDR transfer {media.color_transfer!r} is not supported by smart mode."
    if support is not None and not support.runnable:
        return support.error_message or f"VMAF model {support.model} is unavailable on {support.backend.value}."
    return None


def _write_analysis_header(
    log_file: TextIO,
    item: EncodePlanItem,
    windows: list[SampleWindow],
    plan: AnalysisExecutionPlan,
) -> None:
    if item.media_info is None:
        raise ValueError("Smart analysis requires probed media.")
    model_spec = select_vmaf_model(item.media_info, item.options.viewing_context)
    metadata = candidate_encode_metadata(item.media_info, item.options.pix_fmt)
    sample_label = (
        f"{len(windows)}x{windows[0].duration_sec:.0f}s" if windows else "0"
    )
    log_file.write(
        "Smart analysis:\n"
        f"source={item.source_path.name}\n"
        f"profile={item.options.analysis_profile.value}\n"
        f"tier={plan.tier.value}\n"
        f"vmaf_generation={model_spec.generation}\n"
        f"vmaf_model={model_spec.name}\n"
        f"viewing_context={item.options.viewing_context.value}\n"
        f"vmaf_hfr={'yes' if model_spec.hfr else 'no'}\n"
        f"vmaf_display={model_spec.display_width}x{model_spec.display_height}\n"
        f"vmaf_measurement={VMAF_MEASUREMENT_PIX_FMT}/{VMAF_MEASUREMENT_BIT_DEPTH}-bit\n"
        f"candidate_encode={metadata.width}x{metadata.height}/{metadata.bit_depth}-bit\n"
        f"source_geometry={item.media_info.width}x{item.media_info.height}\n"
        f"source_bit_depth={item.media_info.bit_depth}\n"
        "pooling=smart_v2_temporal_mean_worst_1s_v1\n"
        f"hardware={plan.analysis_backend}\n"
        f"decode={plan.source_decode_acceleration}\n"
        f"candidate_encoder={plan.encoder_name}\n"
        f"vmaf={plan.vmaf_backend.value}\n"
        f"n_threads={plan.vmaf_threads}\n"
        f"n_subsample={plan.vmaf_subsample}\n"
        f"samples={sample_label}\n"
    )
    log_file.flush()


def analyze_quality(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    workdir: Path,
    log_path: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
    active_cpu_vmaf_jobs: int = 1,
) -> QualitySearchResult:
    if item.options.compression_mode != CompressionMode.SMART:
        raise ValueError("Quality analysis is only available in smart mode.")
    if item.media_info is None or item.encoder_info is None:
        raise ValueError("Smart analysis requires probed media and a bound encoder.")

    unsupported = _unsupported_reason(item)
    if unsupported is not None:
        return QualitySearchResult(
            status=QualitySearchStatus.UNSUPPORTED,
            encoder_name=item.encoder_info.encoder_name,
            backend=item.encoder_info.backend,
            reason=unsupported,
        )
    model_spec = select_vmaf_model(item.media_info, item.options.viewing_context)
    runtime_support = select_vmaf_runtime(ffmpeg_path, model_spec)

    analysis_capabilities = detect_analysis_capabilities(ffmpeg_path)
    active_cpu_vmaf_jobs = max(1, int(active_cpu_vmaf_jobs))
    profile = item.options.analysis_settings
    exact_plan = build_analysis_execution_plan(
        tier=AnalysisTier.EXACT,
        encoder_info=item.encoder_info,
        production_preset=item.options.encoder_preset,
        production_two_pass=item.options.two_pass,
        capabilities=analysis_capabilities,
        decode_policy=AnalysisDecodePolicy.AUTO,
        vmaf_backend=runtime_support.backend,
        active_cpu_vmaf_jobs=active_cpu_vmaf_jobs,
        coarse_vmaf_subsample=profile.coarse_vmaf_subsample,
        exact_vmaf_subsample=profile.exact_vmaf_subsample,
    )
    fingerprint = quality_configuration_fingerprint(
        ffmpeg_path,
        item,
        vmaf_backend=exact_plan.vmaf_backend,
        vmaf_subsample=exact_plan.vmaf_subsample,
    )
    measurement_fingerprint = measurement_configuration_fingerprint(
        ffmpeg_path,
        item,
        vmaf_backend=exact_plan.vmaf_backend,
        vmaf_subsample=exact_plan.vmaf_subsample,
    )
    if not runtime_support.runnable:
        return QualitySearchResult(
            status=QualitySearchStatus.UNSUPPORTED,
            encoder_name=item.encoder_info.encoder_name,
            backend=item.encoder_info.backend,
            measurement_fingerprint=measurement_fingerprint,
            fingerprint=fingerprint,
            reason=_unsupported_reason(item, runtime_support),
        )
    cached = item.quality_search_result
    if cached is not None and cached.fingerprint == fingerprint:
        return cached
    initial_candidates: list[QualityCandidateResult] = []
    completed_search_fingerprint = ""
    receipt: AnalysisReceipt | None = None
    if cached is not None and cached.measurement_fingerprint == measurement_fingerprint:
        initial_candidates = list(cached.candidates)
    else:
        receipt = load_analysis_receipt(workdir, measurement_fingerprint)
        if (
            receipt is not None
            and receipt.sample_scheme_version == SMART_SAMPLE_SCHEME_VERSION
        ):
            initial_candidates = list(receipt.candidates)
            completed_search_fingerprint = receipt.search_fingerprint

    budget = calculate_smart_bitrate_budget(item)
    if budget.max_video_bitrate_bps < budget.min_video_bitrate_bps:
        return reselect_from_candidates(
            initial_candidates,
            item,
            measurement_fingerprint=measurement_fingerprint,
            fingerprint=fingerprint,
        )

    if initial_candidates:
        reused = reselect_from_candidates(
            initial_candidates,
            item,
            measurement_fingerprint=measurement_fingerprint,
            fingerprint=fingerprint,
        )
        if completed_search_fingerprint == fingerprint:
            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "analysis",
                        "state": "receipt_loaded",
                        "reused_candidate_count": len(initial_candidates),
                        "file_name": item.source_path.name,
                        "file_path": str(item.source_path),
                    }
                )
            return reused

    if progress_callback is not None and initial_candidates:
        progress_callback(
            {
                "stage": "analysis",
                "state": "receipt_loaded",
                "reused_candidate_count": len(initial_candidates),
                "file_name": item.source_path.name,
                "file_path": str(item.source_path),
            }
        )

    workdir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="smart-analysis-", dir=workdir) as temp_dir:
        temp_root = Path(temp_dir)
        scout_observations: list[ScoutObservation]
        if receipt is not None and receipt.search_windows:
            planned_search = [planned_window_from_payload(value) for value in receipt.search_windows]
            planned_holdouts = [planned_window_from_payload(value) for value in receipt.holdout_windows]
            planned_reserves = [planned_window_from_payload(value) for value in receipt.reserve_windows]
            scout_observations = [
                scout_observation_from_payload(value) for value in receipt.scout_windows
            ]
            sample_plan = SamplePlan(
                scout_windows=tuple(value.window for value in scout_observations),
                search_windows=tuple(planned_search),
                holdout_windows=tuple(planned_holdouts),
                whole_video=any("whole_video" in window.reasons for window in planned_search),
                reserve_windows=tuple(planned_reserves),
                content_uncertainty=receipt.content_uncertainty,
                content_heterogeneity=receipt.content_heterogeneity,
            )
        else:
            sample_plan = SamplePlan((), (), (), False)
            scout_observations = []
        coarse_plan = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=item.encoder_info,
            production_preset=item.options.encoder_preset,
            production_two_pass=item.options.two_pass,
            capabilities=analysis_capabilities,
            decode_policy=AnalysisDecodePolicy.AUTO,
            vmaf_backend=runtime_support.backend,
            active_cpu_vmaf_jobs=active_cpu_vmaf_jobs,
            coarse_vmaf_subsample=profile.coarse_vmaf_subsample,
            exact_vmaf_subsample=profile.exact_vmaf_subsample,
        )
        with log_path.open("a", encoding="utf-8") as log_file:
            if not sample_plan.search_windows:
                def run_sampling_command(command: list[str], phase: str) -> None:
                    _run_logged(
                        command,
                        log_file,
                        cancel_check=cancel_check,
                        process_callback=process_callback,
                        phase=phase,
                    )

                try:
                    sampling = discover_sample_plan(
                        ffmpeg_path=ffmpeg_path,
                        source_path=item.source_path,
                        source_duration_sec=item.media_info.duration,
                        settings=profile,
                        temp_root=temp_root,
                        run_command=run_sampling_command,
                        progress=lambda state, values: emit_analysis_progress(
                            progress_callback, item, state, **values
                        ),
                    )
                    sample_plan = sampling.plan
                    scout_observations = list(sampling.observations)
                except OperationCancelledError:
                    raise
                except (
                    ComplexityProbeError,
                    SamplePlanningError,
                    SmartCommandError,
                    RuntimeError,
                ) as exc:
                    return QualitySearchResult(
                        status=QualitySearchStatus.FAILED,
                        encoder_name=item.encoder_info.encoder_name,
                        backend=item.encoder_info.backend,
                        measurement_fingerprint=measurement_fingerprint,
                        fingerprint=fingerprint,
                        reason=f"Smart content scout failed: {exc}",
                    )
            planned_search = list(sample_plan.search_windows)
            planned_holdouts = list(sample_plan.holdout_windows)
            planned_reserves = list(sample_plan.reserve_windows)
            windows = [sample_window(window) for window in planned_search]
            _write_analysis_header(log_file, item, windows, exact_plan)
            log_file.write(
                f"scout_windows={len(scout_observations)}\n"
                f"search_windows={len(planned_search)}\n"
                f"holdout_windows={len(planned_holdouts)}\n"
                f"reserve_windows={len(planned_reserves)}\n"
            )
            for window in [*planned_search, *planned_holdouts, *planned_reserves]:
                log_file.write(
                    f"sample={window.id} start={window.start_sec:.3f} duration={window.duration_sec:.3f} "
                    f"reasons={','.join(window.reasons)} crosses_scene_cut={window.crosses_scene_cut}\n"
                )
            log_file.flush()

            session = AnalysisSession(
                ffmpeg_path=ffmpeg_path, item=item, workdir=workdir,
                temp_root=temp_root, log_file=log_file, budget=budget,
                exact_plan=exact_plan, coarse_plan=coarse_plan,
                planned_search=planned_search, scout_observations=scout_observations,
                initial_candidates=initial_candidates, progress_callback=progress_callback,
                cancel_check=cancel_check, process_callback=process_callback,
            )
            search = run_search(
                session, sample_plan, measurement_fingerprint=measurement_fingerprint, fingerprint=fingerprint,
            )
            _log_timing(log_file, f"Smart total: {time.perf_counter() - started:.2f}s")

    if session.exact_plan.vmaf_backend != VmafBackend.CPU or session.exact_plan.fallback_reason:
        measurement_fingerprint = measurement_configuration_fingerprint(
            ffmpeg_path,
            item,
            vmaf_backend=session.exact_plan.vmaf_backend,
            vmaf_subsample=session.exact_plan.vmaf_subsample,
        )
        fingerprint = quality_configuration_fingerprint(
            ffmpeg_path,
            item,
            vmaf_backend=session.exact_plan.vmaf_backend,
            vmaf_subsample=session.exact_plan.vmaf_subsample,
        )

    search.candidates = _refresh_candidate_predictions(search.candidates, budget, item.media_info.duration)
    persistable = complete_candidates(search.candidates, len(session.windows))
    search_min_vmaf = search.selection.min_vmaf
    completed_fingerprint = (
        fingerprint if search.terminal_result is None and search.selection.success else ""
    )
    if persistable:
        try:
            save_analysis_receipt(
                workdir,
                _analysis_receipt(
                    ffmpeg_path,
                    item,
                    measurement_fingerprint,
                    session.windows,
                    persistable,
                    scout_observations=scout_observations,
                    search_windows=session.planned_search,
                    holdout_windows=search.remaining_holdouts,
                    refinement_rounds=search.refinement_records,
                    search_min_vmaf=search_min_vmaf,
                    holdout_min_vmaf=search.holdout_min_vmaf,
                    vmaf_backend=session.exact_plan.vmaf_backend,
                    vmaf_subsample=session.exact_plan.vmaf_subsample,
                    search_fingerprint=completed_fingerprint,
                    reserve_windows=search.remaining_reserves,
                    content_uncertainty=sample_plan.content_uncertainty,
                    content_heterogeneity=sample_plan.content_heterogeneity,
                    independent_final_holdout=bool(
                        search.remaining_holdouts
                        and any(
                            window.scout_id not in search.search_history_scout_ids
                            for window in search.remaining_holdouts
                        )
                    ),
                    adaptive_expansion_events=search.adaptive_expansion_events,
                    rd_ambiguity_events=search.ambiguity_records,
                    size_calibration_windows=search.size_calibration_records,
                ),
            )
        except (OSError, ValueError) as exc:
            if progress_callback is not None:
                progress_callback(
                    {
                        "stage": "analysis",
                        "state": "receipt_write_failed",
                        "message": str(exc),
                        "file_name": item.source_path.name,
                        "file_path": str(item.source_path),
                    }
                )
    if search.terminal_result is not None:
        return replace(
            search.terminal_result,
            candidates=persistable or search.terminal_result.candidates,
            measurement_fingerprint=measurement_fingerprint,
            fingerprint=fingerprint,
        )
    return reselect_from_candidates(
        persistable or search.candidates,
        item,
        measurement_fingerprint=measurement_fingerprint,
        fingerprint=fingerprint,
    )
