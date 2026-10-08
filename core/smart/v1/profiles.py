from __future__ import annotations

from dataclasses import asdict, replace

from core.models import AnalysisProfileName, AnalysisProfileSettings, EncodeOptions
from .vmaf import validate_vmaf_subsample


FACTORY_ANALYSIS_PROFILES: dict[AnalysisProfileName, AnalysisProfileSettings] = {
    AnalysisProfileName.FAST: AnalysisProfileSettings(
        whole_video_max_sec=12.0,
        scout_duration_sec=1.5,
        scout_multiplier=3,
        scout_max_windows=12,
        sample_duration_sec=4.0,
        sample_count_under_10m=3,
        sample_count_10_to_60m=3,
        sample_count_60_to_180m=4,
        sample_count_over_180m=4,
        holdout_window_count=1,
        holdout_window_count_over_180m=1,
        coarse_max_candidates=3,
        exact_max_candidates=2,
        coarse_vmaf_subsample=5,
        exact_vmaf_subsample=1,
        min_search_tolerance_bps=100_000,
        search_tolerance_ratio=0.06,
        max_refinement_rounds=1,
        preferred_vmaf_margin=0.2,
        search_min_windows=2,
        search_max_windows=4,
        holdout_target_min=1,
        holdout_target_max=1,
        reserve_window_count=1,
    ),
    AnalysisProfileName.BALANCE: AnalysisProfileSettings(),
    AnalysisProfileName.PRECISE: AnalysisProfileSettings(
        whole_video_max_sec=30.0,
        scout_duration_sec=2.5,
        scout_multiplier=6,
        scout_max_windows=64,
        sample_duration_sec=6.0,
        sample_count_under_10m=6,
        sample_count_10_to_60m=7,
        sample_count_60_to_180m=8,
        sample_count_over_180m=10,
        holdout_window_count=3,
        holdout_window_count_over_180m=4,
        coarse_max_candidates=5,
        exact_max_candidates=4,
        coarse_vmaf_subsample=3,
        exact_vmaf_subsample=1,
        min_search_tolerance_bps=25_000,
        search_tolerance_ratio=0.015,
        max_refinement_rounds=2,
        preferred_vmaf_margin=0.5,
        search_min_windows=4,
        search_max_windows=10,
        holdout_target_min=2,
        holdout_target_max=4,
        reserve_window_count=4,
    ),
}


def parse_analysis_profile_name(value: object) -> AnalysisProfileName:
    if isinstance(value, AnalysisProfileName):
        return value
    try:
        return AnalysisProfileName(str(value))
    except ValueError:
        return AnalysisProfileName.BALANCE


def _odd_subsample(value: object, default: int) -> int:
    try:
        raw = int(float(str(value)))
    except (TypeError, ValueError):
        raw = default
    raw = max(1, raw)
    if raw % 2 == 0:
        raw -= 1
    return validate_vmaf_subsample(max(1, raw))


def validate_analysis_settings(settings: AnalysisProfileSettings) -> AnalysisProfileSettings:
    return replace(
        settings,
        whole_video_max_sec=max(1.0, float(settings.whole_video_max_sec)),
        scout_duration_sec=max(0.5, float(settings.scout_duration_sec)),
        scout_multiplier=min(10, max(1, int(settings.scout_multiplier))),
        scout_max_windows=min(128, max(1, int(settings.scout_max_windows))),
        sample_duration_sec=max(1.0, float(settings.sample_duration_sec)),
        sample_count_under_10m=min(16, max(1, int(settings.sample_count_under_10m))),
        sample_count_10_to_60m=min(16, max(1, int(settings.sample_count_10_to_60m))),
        sample_count_60_to_180m=min(16, max(1, int(settings.sample_count_60_to_180m))),
        sample_count_over_180m=min(16, max(1, int(settings.sample_count_over_180m))),
        holdout_window_count=min(8, max(0, int(settings.holdout_window_count))),
        holdout_window_count_over_180m=min(
            8, max(0, int(settings.holdout_window_count_over_180m))
        ),
        coarse_max_candidates=min(8, max(1, int(settings.coarse_max_candidates))),
        exact_max_candidates=min(8, max(2, int(settings.exact_max_candidates))),
        coarse_vmaf_subsample=_odd_subsample(settings.coarse_vmaf_subsample, 3),
        exact_vmaf_subsample=_odd_subsample(settings.exact_vmaf_subsample, 1),
        min_search_tolerance_bps=max(1_000, int(settings.min_search_tolerance_bps)),
        search_tolerance_ratio=min(0.25, max(0.005, float(settings.search_tolerance_ratio))),
        max_refinement_rounds=min(4, max(0, int(settings.max_refinement_rounds))),
        preferred_vmaf_margin=min(5.0, max(0.0, float(settings.preferred_vmaf_margin))),
        search_min_windows=min(16, max(1, int(settings.search_min_windows))),
        search_max_windows=min(16, max(int(settings.search_min_windows), int(settings.search_max_windows))),
        holdout_target_min=min(8, max(0, int(settings.holdout_target_min))),
        holdout_target_max=min(8, max(int(settings.holdout_target_min), int(settings.holdout_target_max))),
        reserve_window_count=min(8, max(0, int(settings.reserve_window_count))),
        quality_confidence_band=min(2.0, max(0.1, float(settings.quality_confidence_band))),
    )


def resolve_analysis_settings(
    name: AnalysisProfileName,
    stored_profiles: object = None,
) -> AnalysisProfileSettings:
    # Old custom fields are ignored: profiles are versioned confidence modes.
    del stored_profiles
    return validate_analysis_settings(FACTORY_ANALYSIS_PROFILES[name])


def analysis_profiles_from_config(data: dict[str, object]) -> tuple[AnalysisProfileName, AnalysisProfileSettings]:
    name = parse_analysis_profile_name(data.get("analysis_profile", AnalysisProfileName.BALANCE.value))
    return name, resolve_analysis_settings(name)


def analysis_settings_payload(settings: AnalysisProfileSettings) -> dict[str, float | int]:
    return asdict(validate_analysis_settings(settings))


def all_analysis_profile_payloads(stored_profiles: object = None) -> dict[str, dict[str, float | int]]:
    del stored_profiles
    return {
        name.value: analysis_settings_payload(resolve_analysis_settings(name))
        for name in AnalysisProfileName
    }


def bind_analysis_profile(
    options: EncodeOptions,
    *,
    name: object = None,
    stored_profiles: object = None,
) -> EncodeOptions:
    parsed = parse_analysis_profile_name(name if name is not None else options.analysis_profile)
    return replace(
        options,
        analysis_profile=parsed,
        analysis_settings=resolve_analysis_settings(parsed, stored_profiles),
    )
