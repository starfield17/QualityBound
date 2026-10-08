"""Robust whole-video size estimates from sampled encoder behavior."""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

from core.models import SizePrediction


SIZE_PREDICTION_VERSION = 2
_CONTAINER_BUDGET_FACTOR = 0.98


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _linear_ratio_fit(
    sample_risks: Sequence[float], ratios: Sequence[float]
) -> tuple[float, float]:
    if len(sample_risks) < 2 or len(set(sample_risks)) < 2:
        return statistics.median(ratios), 0.0
    slopes = [
        (ratios[right] - ratios[left]) / (sample_risks[right] - sample_risks[left])
        for left in range(len(ratios))
        for right in range(left + 1, len(ratios))
        if abs(sample_risks[right] - sample_risks[left]) > 1e-9
    ]
    slope = _clamp(statistics.median(slopes), -0.75, 0.75) if slopes else 0.0
    intercept = statistics.median(
        ratio - slope * risk for risk, ratio in zip(sample_risks, ratios)
    )
    return intercept, slope


def predict_size_distribution(
    *,
    requested_bitrate_bps: int,
    observed_window_bitrates: Sequence[int],
    duration_sec: float,
    audio_bitrate_bps: int,
    source_bytes: int | None,
    sample_risks: Sequence[float] = (),
    timeline_risks: Sequence[float] = (),
) -> SizePrediction:
    """Estimate timeline-average bitrate; hard samples never become a max extrapolation."""

    if requested_bitrate_bps <= 0 or duration_sec <= 0:
        raise ValueError("Size prediction requires positive bitrate and duration.")
    observed = [max(0, int(value)) for value in observed_window_bitrates]
    if not observed:
        observed = [requested_bitrate_bps]
    ratios = [_clamp(value / requested_bitrate_bps, 0.0001, 4.0) for value in observed]
    use_fit = len(sample_risks) == len(ratios) and bool(timeline_risks)
    if use_fit:
        intercept, slope = _linear_ratio_fit(sample_risks, ratios)
        timeline_ratios = [_clamp(intercept + slope * risk, 0.0001, 4.0) for risk in timeline_risks]
        central_ratio = statistics.fmean(timeline_ratios)
        fitted = [_clamp(intercept + slope * risk, 0.0001, 4.0) for risk in sample_risks]
        method = "risk_distribution_clipped_theil_sen"
        heterogeneity = statistics.pstdev(timeline_risks) if len(timeline_risks) > 1 else 0.0
    else:
        central_ratio = statistics.median(ratios)
        fitted = [central_ratio] * len(ratios)
        method = "robust_median_window_ratio"
        heterogeneity = 0.0
    residuals = [abs(actual - predicted) for actual, predicted in zip(ratios, fitted)]
    mad = statistics.median(residuals) if residuals else 0.0
    uncertainty = _clamp(0.03 + 0.14 / math.sqrt(len(ratios)) + 1.5 * mad + 0.20 * heterogeneity, 0.05, 0.30)
    mean_bitrate = max(1, round(requested_bitrate_bps * central_ratio))
    upper_bitrate = max(mean_bitrate, round(mean_bitrate * (1.0 + uncertainty)))
    predicted_bytes = math.ceil(
        (upper_bitrate + max(0, audio_bitrate_bps))
        * duration_sec
        / 8.0
        / _CONTAINER_BUDGET_FACTOR
    )
    return SizePrediction(
        mean_video_bitrate_bps=mean_bitrate,
        upper_video_bitrate_bps=upper_bitrate,
        predicted_output_bytes=predicted_bytes,
        predicted_output_ratio=(predicted_bytes / source_bytes if source_bytes and source_bytes > 0 else None),
        uncertainty=uncertainty,
        method=method,
    )
