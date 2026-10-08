"""Cache identity and receipt construction for Smart analysis."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Sequence, cast

from .receipts import ANALYSIS_RECEIPT_SCHEMA_VERSION
from core.models import AnalysisReceipt, EncodePlanItem, QualityCandidateResult, VmafBackend
from .bitrate import resolve_max_output_ratio
from .sampling.planner import (
    PlannedWindow,
    ScoutObservation,
    planned_window_payload,
    ranked_scout_payloads,
)
from .vmaf import (
    EXACT_VMAF_SUBSAMPLE,
    VMAF_ASPECT_POLICY,
    VMAF_MEASUREMENT_BIT_DEPTH,
    VMAF_MEASUREMENT_PIPELINE_VERSION,
    VMAF_MEASUREMENT_PIX_FMT,
    VMAF_MODEL_GENERATION,
    VMAF_RESOLUTION_MODE,
    VMAF_SCALE_FLAGS,
    candidate_encode_metadata,
    select_vmaf_model,
    TEMPORAL_DROP_TOLERANCE,
)
from .size_prediction import SIZE_PREDICTION_VERSION


# These version values are part of the persisted receipt identity.  Keep them
# stable for behavior-preserving refactors.
SMART_SAMPLE_SCHEME_VERSION = 7
SMART_ANALYSIS_ALGORITHM_VERSION = 9
SMART_RISK_VECTOR_VERSION = 2
SMART_ADAPTIVE_PLANNER_VERSION = 3


class _SampleWindow(Protocol):
    @property
    def start_sec(self) -> float: ...

    @property
    def duration_sec(self) -> float: ...


def path_identity(path: Path) -> dict[str, object]:
    try:
        stat = path.stat()
        return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"path": str(path), "size": None, "mtime_ns": None}


def measurement_configuration_payload(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    *,
    vmaf_backend: VmafBackend = VmafBackend.CPU,
    vmaf_subsample: int = EXACT_VMAF_SUBSAMPLE,
) -> dict[str, object]:
    if item.encoder_info is None:
        raise ValueError("Smart analysis requires a bound encoder.")
    if item.media_info is None:
        raise ValueError("Smart analysis requires probed media.")
    options = item.options
    media = item.media_info
    model_spec = select_vmaf_model(media, options.viewing_context)
    encode_metadata = candidate_encode_metadata(media, options.pix_fmt)
    return {
        "source": path_identity(item.source_path),
        "ffmpeg": path_identity(ffmpeg_path),
        "codec": options.codec.value,
        "encoder": item.encoder_info.encoder_name,
        "backend": item.encoder_info.backend.value,
        "preset": options.encoder_preset,
        "pix_fmt": options.pix_fmt,
        "two_pass": options.two_pass,
        "maxrate_factor": options.maxrate_factor,
        "bufsize_factor": options.bufsize_factor,
        "sample_scheme_version": SMART_SAMPLE_SCHEME_VERSION,
        "whole_video_max_sec": options.analysis_settings.whole_video_max_sec,
        "scout_duration_sec": options.analysis_settings.scout_duration_sec,
        "scout_multiplier": options.analysis_settings.scout_multiplier,
        "scout_max_windows": options.analysis_settings.scout_max_windows,
        "sample_duration_sec": options.analysis_settings.sample_duration_sec,
        "sample_count_under_10m": options.analysis_settings.sample_count_under_10m,
        "sample_count_10_to_60m": options.analysis_settings.sample_count_10_to_60m,
        "sample_count_60_to_180m": options.analysis_settings.sample_count_60_to_180m,
        "sample_count_over_180m": options.analysis_settings.sample_count_over_180m,
        "holdout_window_count": options.analysis_settings.holdout_window_count,
        "holdout_window_count_over_180m": options.analysis_settings.holdout_window_count_over_180m,
        "search_min_windows": options.analysis_settings.search_min_windows,
        "search_max_windows": options.analysis_settings.search_max_windows,
        "holdout_target_min": options.analysis_settings.holdout_target_min,
        "holdout_target_max": options.analysis_settings.holdout_target_max,
        "reserve_window_count": options.analysis_settings.reserve_window_count,
        "max_refinement_rounds": options.analysis_settings.max_refinement_rounds,
        "source_width": media.width,
        "source_height": media.height,
        "source_fps": media.fps,
        "source_pix_fmt": media.pix_fmt,
        "source_bit_depth": media.bit_depth,
        "candidate_encode_width": encode_metadata.width,
        "candidate_encode_height": encode_metadata.height,
        "candidate_encode_bit_depth": encode_metadata.bit_depth,
        "vmaf_generation": VMAF_MODEL_GENERATION,
        "vmaf_resolution_mode": VMAF_RESOLUTION_MODE,
        "viewing_context": options.viewing_context.value,
        "vmaf_pooling": "smart_v2_temporal_mean_worst_1s_v1",
        "temporal_drop_tolerance": TEMPORAL_DROP_TOLERANCE,
        "risk_vector_version": SMART_RISK_VECTOR_VERSION,
        "adaptive_planner_version": SMART_ADAPTIVE_PLANNER_VERSION,
        "size_prediction_version": SIZE_PREDICTION_VERSION,
        "vmaf_model": model_spec.name,
        "vmaf_hfr": model_spec.hfr,
        "vmaf_display_width": model_spec.display_width,
        "vmaf_display_height": model_spec.display_height,
        "vmaf_measurement_pix_fmt": VMAF_MEASUREMENT_PIX_FMT,
        "vmaf_measurement_bit_depth": VMAF_MEASUREMENT_BIT_DEPTH,
        "vmaf_measurement_pipeline_version": VMAF_MEASUREMENT_PIPELINE_VERSION,
        "vmaf_scale_algorithm": VMAF_SCALE_FLAGS,
        "vmaf_aspect_policy": VMAF_ASPECT_POLICY,
        "vmaf_subsample": int(vmaf_subsample),
        "vmaf_backend": vmaf_backend.value,
        "analysis_algorithm_version": SMART_ANALYSIS_ALGORITHM_VERSION,
    }


def measurement_configuration_fingerprint(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    *,
    vmaf_backend: VmafBackend = VmafBackend.CPU,
    vmaf_subsample: int = EXACT_VMAF_SUBSAMPLE,
) -> str:
    payload = measurement_configuration_payload(
        ffmpeg_path, item, vmaf_backend=vmaf_backend, vmaf_subsample=vmaf_subsample
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def quality_configuration_fingerprint(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    *,
    vmaf_backend: VmafBackend = VmafBackend.CPU,
    vmaf_subsample: int = EXACT_VMAF_SUBSAMPLE,
    measurement_fingerprint: str | None = None,
) -> str:
    options = item.options
    measurement_key = measurement_fingerprint or measurement_configuration_fingerprint(
        ffmpeg_path, item, vmaf_backend=vmaf_backend, vmaf_subsample=vmaf_subsample
    )
    settings = options.analysis_settings
    payload = {
        "measurement_fingerprint": measurement_key,
        "min_vmaf": options.min_vmaf,
        "max_output_ratio": resolve_max_output_ratio(options.codec, options.max_output_ratio),
        "audio_mode": options.audio_mode.value,
        "audio_bitrate": options.audio_bitrate,
        "min_video_kbps": options.min_video_kbps,
        "max_video_kbps": options.max_video_kbps,
        "container": options.container.value,
        "coarse_max_candidates": settings.coarse_max_candidates,
        "exact_max_candidates": settings.exact_max_candidates,
        "coarse_vmaf_subsample": settings.coarse_vmaf_subsample,
        "exact_vmaf_subsample": settings.exact_vmaf_subsample,
        "min_search_tolerance_bps": settings.min_search_tolerance_bps,
        "search_tolerance_ratio": settings.search_tolerance_ratio,
        "preferred_vmaf_margin": settings.preferred_vmaf_margin,
        "analysis_algorithm_version": SMART_ANALYSIS_ALGORITHM_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def analysis_receipt(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    measurement_fingerprint: str,
    windows: Sequence[_SampleWindow],
    candidates: list[QualityCandidateResult],
    *,
    scout_observations: list[ScoutObservation],
    search_windows: list[PlannedWindow],
    holdout_windows: list[PlannedWindow],
    refinement_rounds: list[dict[str, object]],
    search_min_vmaf: float | None,
    holdout_min_vmaf: float | None,
    vmaf_backend: VmafBackend,
    vmaf_subsample: int,
    search_fingerprint: str,
    reserve_windows: list[PlannedWindow] | None = None,
    content_uncertainty: float = 0.0,
    content_heterogeneity: float = 0.0,
    independent_final_holdout: bool = False,
    adaptive_expansion_events: list[dict[str, object]] | None = None,
    rd_ambiguity_events: list[dict[str, object]] | None = None,
    size_calibration_windows: list[dict[str, object]] | None = None,
) -> AnalysisReceipt:
    if item.encoder_info is None:
        raise ValueError("Smart analysis receipt requires a bound encoder.")
    payload = measurement_configuration_payload(
        ffmpeg_path, item, vmaf_backend=vmaf_backend, vmaf_subsample=vmaf_subsample
    )
    return AnalysisReceipt(
        schema_version=ANALYSIS_RECEIPT_SCHEMA_VERSION,
        measurement_fingerprint=measurement_fingerprint,
        source_identity=dict(cast(dict[str, object], payload["source"])),
        ffmpeg_identity=dict(cast(dict[str, object], payload["ffmpeg"])),
        encoder_identity={
            "codec": item.options.codec.value,
            "backend": item.encoder_info.backend.value,
            "encoder": item.encoder_info.encoder_name,
            "preset": item.options.encoder_preset,
        },
        sample_scheme_version=SMART_SAMPLE_SCHEME_VERSION,
        sample_windows=[(window.start_sec, window.duration_sec) for window in windows],
        scout_windows=ranked_scout_payloads(scout_observations) if scout_observations else [],
        search_windows=[planned_window_payload(value) for value in search_windows],
        holdout_windows=[planned_window_payload(value) for value in holdout_windows],
        reserve_windows=[planned_window_payload(value) for value in (reserve_windows or [])],
        refinement_rounds=refinement_rounds,
        search_min_vmaf=search_min_vmaf,
        holdout_min_vmaf=holdout_min_vmaf,
        search_fingerprint=search_fingerprint,
        measurement_configuration={key: value for key, value in payload.items() if key not in {"source", "ffmpeg"}},
        candidates=candidates,
        created_at=datetime.now(timezone.utc).isoformat(),
        content_uncertainty=content_uncertainty,
        content_heterogeneity=content_heterogeneity,
        adaptive_expansion_events=adaptive_expansion_events or [],
        rd_ambiguity_events=rd_ambiguity_events or [],
        size_calibration_windows=size_calibration_windows or [],
        viewing_context=item.options.viewing_context.value,
        selected_vmaf_model=str(payload["vmaf_model"]),
        independent_final_holdout=independent_final_holdout,
    )
