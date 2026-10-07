"""Pure bitrate budgeting and candidate-selection rules for Smart analysis."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, TypedDict

from core.models import (
    AudioMode,
    BackendChoice,
    CodecChoice,
    ConstraintFailureKind,
    EncodePlanItem,
    QualityCandidateResult,
    QualitySearchResult,
    QualitySearchStatus,
)
from .size_prediction import predict_size_distribution


DEFAULT_MAX_OUTPUT_RATIO = {
    CodecChoice.HEVC: 0.70,
    CodecChoice.AV1: 0.50,
}
MAX_SEARCH_CANDIDATES = 8
CONTAINER_BUDGET_FACTOR = 0.98


@dataclass(frozen=True, slots=True)
class SmartBitrateBudget:
    source_bytes: int
    max_output_bytes: int
    audio_bitrate_bps: int
    min_video_bitrate_bps: int
    max_video_bitrate_bps: int


def resolve_max_output_ratio(codec: CodecChoice, configured: float | None) -> float:
    ratio = DEFAULT_MAX_OUTPUT_RATIO[codec] if configured is None else float(configured)
    if not 0 < ratio <= 1:
        raise ValueError("max_output_ratio must be greater than 0 and at most 1")
    return ratio


def parse_bitrate_bps(raw: str) -> int:
    value = raw.strip().lower()
    multiplier = 1
    if value.endswith("k"):
        value, multiplier = value[:-1], 1_000
    elif value.endswith("m"):
        value, multiplier = value[:-1], 1_000_000
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid audio bitrate: {raw}") from exc
    if parsed <= 0:
        raise ValueError("Audio bitrate must be greater than 0.")
    return int(round(parsed * multiplier))


def calculate_smart_bitrate_budget(item: EncodePlanItem) -> SmartBitrateBudget:
    if item.media_info is None:
        raise ValueError("Smart analysis requires media information.")
    source_bytes = item.source_path.stat().st_size
    ratio = resolve_max_output_ratio(item.options.codec, item.options.max_output_ratio)
    max_output_bytes = max(1, math.floor(source_bytes * ratio))
    duration = item.media_info.duration
    total_media_bitrate = math.floor(max_output_bytes * 8 * CONTAINER_BUDGET_FACTOR / duration)

    if item.options.audio_mode == AudioMode.COPY:
        audio_bitrate = max(0, int(item.media_info.audio_bitrate_bps))
    else:
        stream_count = max(0, int(item.media_info.audio_stream_count or 0))
        audio_bitrate = parse_bitrate_bps(item.options.audio_bitrate) * stream_count

    max_video_bitrate = total_media_bitrate - audio_bitrate
    configured_max = int(item.options.max_video_kbps) * 1_000
    if configured_max > 0:
        max_video_bitrate = min(max_video_bitrate, configured_max)
    return SmartBitrateBudget(
        source_bytes=source_bytes,
        max_output_bytes=max_output_bytes,
        audio_bitrate_bps=audio_bitrate,
        min_video_bitrate_bps=max(1, int(item.options.min_video_kbps) * 1_000),
        max_video_bitrate_bps=max_video_bitrate,
    )


def predicted_output_size(
    video_bitrate_bps: int,
    audio_bitrate_bps: int,
    duration_sec: float,
) -> int:
    media_bytes = (video_bitrate_bps + audio_bitrate_bps) * duration_sec / 8.0
    return int(math.ceil(media_bytes / CONTAINER_BUDGET_FACTOR))


def _floor_candidate(value: int) -> int:
    return max(1_000, int(value // 1_000) * 1_000)


def _ceil_candidate(value: int) -> int:
    return max(1_000, int(math.ceil(value / 1_000.0)) * 1_000)


def search_bitrate_candidates(
    *,
    evaluate: Callable[[int], QualityCandidateResult],
    min_bitrate_bps: int,
    budget_bitrate_bps: int,
    required_search_ceiling_bps: int,
    min_vmaf: float,
    max_candidates: int = MAX_SEARCH_CANDIDATES,
    max_output_bytes: int | None = None,
    initial_candidates: list[QualityCandidateResult] | None = None,
    tolerance_bps: int | None = None,
    preferred_first_bitrate_bps: int | None = None,
) -> tuple[list[QualityCandidateResult], int | None, int | None]:
    """Return tested candidates, a selectable bitrate, and required bitrate."""
    cache: dict[int, QualityCandidateResult] = {
        candidate.video_bitrate_bps: candidate for candidate in (initial_candidates or [])
    }
    candidate_limit = min(MAX_SEARCH_CANDIDATES, max(0, int(max_candidates)))
    stop_delta = max(1_000, int(tolerance_bps) if tolerance_bps is not None else 1_000)
    evaluated = 0

    def quality_passes(result: QualityCandidateResult) -> bool:
        return result.min_vmaf >= min_vmaf

    def constraints_pass(result: QualityCandidateResult) -> bool:
        if not quality_passes(result):
            return False
        if max_output_bytes is None:
            return True
        predicted_bytes = result.predicted_output_bytes
        return predicted_bytes is not None and predicted_bytes <= max_output_bytes

    def summarize(*, allow_selected: bool) -> tuple[int | None, int | None]:
        quality_passing = [result.video_bitrate_bps for result in cache.values() if quality_passes(result)]
        required = min(quality_passing) if quality_passing else None
        if not allow_selected:
            return None, required
        selectable = [result.video_bitrate_bps for result in cache.values() if constraints_pass(result)]
        return (min(selectable) if selectable else None), required

    def test(value: int) -> QualityCandidateResult | None:
        nonlocal evaluated
        bitrate = _floor_candidate(value)
        if bitrate in cache:
            return cache.get(bitrate)
        if evaluated >= candidate_limit:
            return None
        cache[bitrate] = evaluate(bitrate)
        evaluated += 1
        return cache[bitrate]

    budget = _floor_candidate(budget_bitrate_bps)
    minimum = _ceil_candidate(min_bitrate_bps)
    if preferred_first_bitrate_bps is not None:
        predicted = min(
            _floor_candidate(max(minimum, preferred_first_bitrate_bps)),
            _floor_candidate(max(required_search_ceiling_bps, budget)),
        )
        predicted_score = test(predicted)
        if predicted_score is not None and quality_passes(predicted_score):
            if predicted_score.min_vmaf >= min_vmaf + 0.8 and evaluated < candidate_limit:
                nearby = max(minimum, predicted - max(stop_delta, (predicted - minimum) // 3))
                test(nearby)
            selected, required = summarize(allow_selected=True)
            return list(cache.values()), selected, required
    upper_score = test(budget)
    if upper_score is None:
        return list(cache.values()), None, None

    if quality_passes(upper_score):
        low_score = test(minimum)
        if low_score is not None and quality_passes(low_score):
            selected, required = summarize(allow_selected=True)
            return list(cache.values()), selected, required
        low = minimum
        high = budget
        while evaluated < candidate_limit and high - low > stop_delta:
            middle = _floor_candidate((low + high) // 2)
            if middle in cache:
                break
            score = test(middle)
            if score is None:
                break
            if quality_passes(score):
                high = middle
            else:
                low = middle
        selected, required = summarize(allow_selected=True)
        return list(cache.values()), selected, required

    ceiling = _floor_candidate(max(required_search_ceiling_bps, budget))
    ceiling_score = test(ceiling) if ceiling > budget else upper_score
    if ceiling_score is None or not quality_passes(ceiling_score):
        return list(cache.values()), None, None

    low = budget
    high = ceiling
    while evaluated < candidate_limit and high - low > stop_delta:
        middle = _floor_candidate((low + high) // 2)
        if middle in cache:
            break
        score = test(middle)
        if score is None:
            break
        if quality_passes(score):
            high = middle
        else:
            low = middle
    selected, required = summarize(allow_selected=max_output_bytes is not None)
    return list(cache.values()), selected, required


def rd_ambiguity_events(
    candidates: list[QualityCandidateResult], *, tolerance: float = 0.5
) -> list[dict[str, object]]:
    ordered = sorted(candidates, key=lambda candidate: candidate.video_bitrate_bps)
    events: list[dict[str, object]] = []
    for lower, higher in zip(ordered, ordered[1:]):
        if higher.min_vmaf < lower.min_vmaf - tolerance:
            events.append(
                {
                    "lower_bitrate_bps": lower.video_bitrate_bps,
                    "lower_quality_score": lower.min_vmaf,
                    "higher_bitrate_bps": higher.video_bitrate_bps,
                    "higher_quality_score": higher.min_vmaf,
                    "tolerance": tolerance,
                }
            )
    return events


def refresh_candidate_predictions(
    candidates: list[QualityCandidateResult],
    budget: SmartBitrateBudget,
    duration_sec: float,
) -> list[QualityCandidateResult]:
    refreshed: list[QualityCandidateResult] = []
    for candidate in candidates:
        if candidate.size_prediction is not None:
            prior = candidate.size_prediction
            predicted_bytes = predicted_output_size(
                prior.upper_video_bitrate_bps,
                budget.audio_bitrate_bps,
                duration_sec,
            )
            prediction = replace(
                prior,
                predicted_output_bytes=predicted_bytes,
                predicted_output_ratio=(predicted_bytes / budget.source_bytes if budget.source_bytes else None),
            )
            refreshed.append(
                replace(
                    candidate,
                    observed_video_bitrate_bps=prediction.mean_video_bitrate_bps,
                    predicted_output_bytes=prediction.predicted_output_bytes,
                    predicted_output_ratio=prediction.predicted_output_ratio,
                    size_prediction=prediction,
                )
            )
            continue
        measured_bitrates = list(candidate.observed_window_bitrates)
        if not measured_bitrates and candidate.encoded_bytes and candidate.encoded_durations_sec:
            measured_bitrates = [
                int(math.ceil(encoded_bytes * 8.0 / duration))
                for encoded_bytes, duration in zip(candidate.encoded_bytes, candidate.encoded_durations_sec)
                if duration > 0
            ]
        if not measured_bitrates and candidate.observed_video_bitrate_bps > 0:
            measured_bitrates = [candidate.observed_video_bitrate_bps]
        if not measured_bitrates and candidate.predicted_output_bytes is not None:
            refreshed.append(candidate)
            continue
        prediction = predict_size_distribution(
            requested_bitrate_bps=candidate.video_bitrate_bps,
            observed_window_bitrates=measured_bitrates,
            duration_sec=duration_sec,
            audio_bitrate_bps=budget.audio_bitrate_bps,
            source_bytes=budget.source_bytes,
        )
        refreshed.append(
            replace(
                candidate,
                observed_window_bitrates=measured_bitrates,
                observed_video_bitrate_bps=prediction.mean_video_bitrate_bps,
                predicted_output_bytes=prediction.predicted_output_bytes,
                predicted_output_ratio=prediction.predicted_output_ratio,
                size_prediction=prediction,
            )
        )
    return refreshed


class _SharedSearchResultFields(TypedDict):
    """Encoder identity and budget carried by every outcome of this selection."""

    encoder_name: str
    backend: BackendChoice
    max_output_bytes: int
    measurement_fingerprint: str
    fingerprint: str


def reselect_from_candidates(
    candidates: list[QualityCandidateResult],
    item: EncodePlanItem,
    *,
    measurement_fingerprint: str = "",
    fingerprint: str = "",
) -> QualitySearchResult:
    if item.media_info is None or item.encoder_info is None:
        raise ValueError("Smart candidate selection requires probed media and a bound encoder.")
    budget = calculate_smart_bitrate_budget(item)
    base: _SharedSearchResultFields = {
        "encoder_name": item.encoder_info.encoder_name,
        "backend": item.encoder_info.backend,
        "measurement_fingerprint": measurement_fingerprint,
        "fingerprint": fingerprint,
        "max_output_bytes": budget.max_output_bytes,
    }
    if budget.max_video_bitrate_bps < budget.min_video_bitrate_bps:
        return QualitySearchResult(
            status=QualitySearchStatus.CONSTRAINT_UNSATISFIED,
            failure_kind=ConstraintFailureKind.MEDIA_BUDGET_TOO_SMALL,
            reason="Audio and container overhead leave too little room for the minimum video bitrate.",
            **base,
        )

    refreshed = refresh_candidate_predictions(candidates, budget, item.media_info.duration)
    configured_max_bps = max(0, int(item.options.max_video_kbps)) * 1_000
    rate_eligible = [
        candidate
        for candidate in refreshed
        if candidate.video_bitrate_bps >= budget.min_video_bitrate_bps
        and (configured_max_bps == 0 or candidate.video_bitrate_bps <= configured_max_bps)
    ]
    quality_passing = [candidate for candidate in rate_eligible if candidate.min_vmaf >= item.options.min_vmaf]
    selectable = [
        candidate
        for candidate in quality_passing
        if candidate.predicted_output_bytes is not None and candidate.predicted_output_bytes <= budget.max_output_bytes
    ]
    size_fitting = [
        candidate
        for candidate in rate_eligible
        if candidate.predicted_output_bytes is not None and candidate.predicted_output_bytes <= budget.max_output_bytes
    ]
    best_size_fitting = max(
        size_fitting,
        key=lambda candidate: (candidate.min_vmaf, -candidate.video_bitrate_bps),
        default=None,
    )
    if selectable:
        preferred_target = item.options.min_vmaf + item.options.analysis_settings.preferred_vmaf_margin
        preferred = [candidate for candidate in selectable if candidate.min_vmaf >= preferred_target]
        chosen = min(preferred or selectable, key=lambda candidate: candidate.video_bitrate_bps)
        return QualitySearchResult(
            status=QualitySearchStatus.FOUND,
            candidates=refreshed,
            selected_video_bitrate_bps=chosen.video_bitrate_bps,
            min_vmaf=chosen.min_vmaf,
            predicted_output_bytes=chosen.predicted_output_bytes,
            predicted_output_ratio=chosen.predicted_output_ratio,
            best_size_fitting_candidate_bps=(best_size_fitting.video_bitrate_bps if best_size_fitting else 0),
            best_size_fitting_vmaf=(best_size_fitting.min_vmaf if best_size_fitting else None),
            **base,
        )

    required = min(quality_passing, key=lambda candidate: candidate.video_bitrate_bps, default=None)
    required_ratio = required.predicted_output_ratio if required is not None else None
    size_blocked = (
        required is not None
        and required.predicted_output_bytes is not None
        and required.predicted_output_bytes > budget.max_output_bytes
    )
    failure_kind = (
        ConstraintFailureKind.SIZE_BLOCKED if size_blocked else ConstraintFailureKind.QUALITY_UNREACHABLE
    )
    if size_blocked and required_ratio is not None:
        reason = (
            f"VMAF {item.options.min_vmaf:.1f} requires an estimated output ratio of "
            f"{required_ratio:.3f}, above the configured size limit."
        )
    else:
        reason = f"The bound encoder cannot reach VMAF {item.options.min_vmaf:.1f} with the tested candidates."
    return QualitySearchResult(
        status=QualitySearchStatus.CONSTRAINT_UNSATISFIED,
        candidates=refreshed,
        selected_video_bitrate_bps=(
            required.video_bitrate_bps
            if required is not None
            else (best_size_fitting.video_bitrate_bps if best_size_fitting is not None else 0)
        ),
        min_vmaf=(required.min_vmaf if required is not None else (best_size_fitting.min_vmaf if best_size_fitting else None)),
        predicted_output_bytes=(
            required.predicted_output_bytes
            if required is not None
            else (best_size_fitting.predicted_output_bytes if best_size_fitting else None)
        ),
        predicted_output_ratio=(
            required.predicted_output_ratio
            if required is not None
            else (best_size_fitting.predicted_output_ratio if best_size_fitting else None)
        ),
        required_output_ratio=required_ratio,
        required_video_bitrate_bps=(required.video_bitrate_bps if required else 0),
        best_size_fitting_candidate_bps=(best_size_fitting.video_bitrate_bps if best_size_fitting else 0),
        best_size_fitting_vmaf=(best_size_fitting.min_vmaf if best_size_fitting else None),
        failure_kind=failure_kind,
        reason=reason,
        **base,
    )
