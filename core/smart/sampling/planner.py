"""Deterministic Smart sample planning from lightweight scout metrics."""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, replace
from typing import Iterable

from core.models import AnalysisProfileSettings


_EPSILON = 1e-9


class SamplePlanningError(ValueError):
    """The scout results cannot produce a valid, non-overlapping sample plan."""


@dataclass(frozen=True, slots=True)
class ScoutWindow:
    id: str
    start_sec: float
    duration_sec: float

    @property
    def center_sec(self) -> float:
        return self.start_sec + self.duration_sec / 2.0


@dataclass(frozen=True, slots=True)
class ScoutObservation:
    window: ScoutWindow
    si_p90: float
    ti_p90: float
    scene_cut_times: tuple[float, ...] = ()
    max_scene_score: float = 0.0
    dark: float = 0.0
    flat_or_gradient: float = 0.0
    high_frequency_or_noise: float = 0.0
    text_edge_like: float = 0.0


@dataclass(frozen=True, slots=True)
class CompressionRiskVector:
    spatial_detail: float
    motion: float
    transition: float
    dark: float
    flat_or_gradient: float
    high_frequency_or_noise: float
    text_edge_like: float = 0.0

    @property
    def global_risk(self) -> float:
        return (
            0.27 * self.spatial_detail
            + 0.30 * self.motion
            + 0.17 * self.transition
            + 0.08 * self.dark
            + 0.08 * self.flat_or_gradient
            + 0.10 * self.high_frequency_or_noise
        )


@dataclass(frozen=True, slots=True)
class PlannedWindow:
    id: str
    start_sec: float
    duration_sec: float
    reasons: tuple[str, ...]
    scout_id: str | None = None
    crosses_scene_cut: bool = False

    @property
    def center_sec(self) -> float:
        return self.start_sec + self.duration_sec / 2.0


@dataclass(frozen=True, slots=True)
class SamplePlan:
    scout_windows: tuple[ScoutWindow, ...]
    search_windows: tuple[PlannedWindow, ...]
    holdout_windows: tuple[PlannedWindow, ...]
    whole_video: bool
    reserve_windows: tuple[PlannedWindow, ...] = ()
    content_uncertainty: float = 0.0
    content_heterogeneity: float = 0.0


def planned_window_payload(window: PlannedWindow) -> dict[str, object]:
    return {
        "id": window.id,
        "start_sec": window.start_sec,
        "duration_sec": window.duration_sec,
        "reasons": list(window.reasons),
        "scout_id": window.scout_id,
        "crosses_scene_cut": window.crosses_scene_cut,
    }


def planned_window_from_payload(data: dict[str, object]) -> PlannedWindow:
    raw_reasons = data.get("reasons", [])
    reasons = tuple(str(value) for value in raw_reasons) if isinstance(raw_reasons, list) else ()
    return PlannedWindow(
        id=str(data["id"]),
        start_sec=float(str(data["start_sec"])),
        duration_sec=float(str(data["duration_sec"])),
        reasons=reasons,
        scout_id=None if data.get("scout_id") is None else str(data["scout_id"]),
        crosses_scene_cut=bool(data.get("crosses_scene_cut", False)),
    )


def scout_observation_payload(observation: ScoutObservation) -> dict[str, object]:
    return {
        "id": observation.window.id,
        "start_sec": observation.window.start_sec,
        "duration_sec": observation.window.duration_sec,
        "si_p90": observation.si_p90,
        "ti_p90": observation.ti_p90,
        "scene_cut_times": list(observation.scene_cut_times),
        "scene_cut_count": len(observation.scene_cut_times),
        "max_scene_score": observation.max_scene_score,
        "dark": observation.dark,
        "flat_or_gradient": observation.flat_or_gradient,
        "high_frequency_or_noise": observation.high_frequency_or_noise,
        "text_edge_like": observation.text_edge_like,
    }


def scout_observation_from_payload(data: dict[str, object]) -> ScoutObservation:
    raw_cuts = data.get("scene_cut_times", [])
    cuts = tuple(float(str(value)) for value in raw_cuts) if isinstance(raw_cuts, list) else ()
    return ScoutObservation(
        window=ScoutWindow(
            id=str(data["id"]),
            start_sec=float(str(data["start_sec"])),
            duration_sec=float(str(data["duration_sec"])),
        ),
        si_p90=float(str(data["si_p90"])),
        ti_p90=float(str(data["ti_p90"])),
        scene_cut_times=cuts,
        max_scene_score=float(str(data.get("max_scene_score", 0.0))),
        dark=float(str(data.get("dark", 0.0))),
        flat_or_gradient=float(str(data.get("flat_or_gradient", 0.0))),
        high_frequency_or_noise=float(str(data.get("high_frequency_or_noise", 0.0))),
        text_edge_like=float(str(data.get("text_edge_like", 0.0))),
    )


@dataclass(frozen=True, slots=True)
class RankedScoutObservation:
    observation: ScoutObservation
    si_rank: float
    ti_rank: float
    difficulty: float
    risk: CompressionRiskVector


def _configured_search_window_count(
    duration_sec: float, settings: AnalysisProfileSettings
) -> int:
    if duration_sec < 10 * 60:
        return int(settings.sample_count_under_10m)
    if duration_sec < 60 * 60:
        return int(settings.sample_count_10_to_60m)
    if duration_sec <= 180 * 60:
        return int(settings.sample_count_60_to_180m)
    return int(settings.sample_count_over_180m)


def search_window_count(duration_sec: float, settings: AnalysisProfileSettings) -> int:
    configured = max(
        settings.search_min_windows,
        min(settings.search_max_windows, _configured_search_window_count(duration_sec, settings)),
    )
    capacity = max(1, int(math.floor(duration_sec / settings.sample_duration_sec)))
    return max(1, min(int(configured), capacity))


def holdout_window_count(duration_sec: float, settings: AnalysisProfileSettings) -> int:
    if duration_sec > 180 * 60:
        return int(settings.holdout_window_count_over_180m)
    return int(settings.holdout_window_count)


def should_analyze_whole_video(
    duration_sec: float, settings: AnalysisProfileSettings
) -> bool:
    """Return whether the selected confidence profile requires full-source analysis."""

    return duration_sec <= settings.whole_video_max_sec


def _unit_hash(seed: str, label: str) -> float:
    return int.from_bytes(hashlib.sha256(f"smart-strata-v1:{seed}:{label}".encode()).digest()[:8], "big") / 2**64


def _blind_window(duration_sec: float, settings: AnalysisProfileSettings, seed: str) -> PlannedWindow:
    duration = min(settings.sample_duration_sec, duration_sec)
    start = (duration_sec - duration) * (0.25 + 0.5 * _unit_hash(seed, "validation"))
    return PlannedWindow("holdout:unscouted", start, duration, ("unscouted_validation",))


def fresh_validation_window(
    duration_sec: float, sample_duration_sec: float,
    occupied: Iterable[tuple[float, float]], *, identity: str,
) -> PlannedWindow | None:
    """Use the largest unobserved gap without consulting measured quality."""
    end = 0.0
    gaps: list[tuple[float, float]] = []
    for start, duration in sorted(occupied):
        if start - end >= sample_duration_sec:
            gaps.append((end, start))
        end = max(end, start + duration)
    if duration_sec - end >= sample_duration_sec:
        gaps.append((end, duration_sec))
    if not gaps:
        return None
    low, high = max(gaps, key=lambda gap: (gap[1] - gap[0], -gap[0]))
    start = (low + high - sample_duration_sec) / 2.0
    return PlannedWindow(identity, start, sample_duration_sec, ("unscouted_validation", "fresh_gap_holdout"))


def plan_scout_windows(
    duration_sec: float, settings: AnalysisProfileSettings, *, seed: str = "",
) -> tuple[ScoutWindow, ...]:
    """Stratify probes around an independently reserved, unobserved interval."""

    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise SamplePlanningError("Source duration must be finite and positive.")
    if should_analyze_whole_video(duration_sec, settings):
        return ()
    required_pools = (
        settings.search_max_windows
        + settings.holdout_target_max
        + settings.reserve_window_count
    )
    requested = min(
        settings.scout_max_windows,
        max(
            required_pools * 2,
            search_window_count(duration_sec, settings) * settings.scout_multiplier,
        ),
    )
    count = max(1, int(requested))
    blind = _blind_window(duration_sec, settings, seed)
    if blind.duration_sec >= duration_sec:
        return ()
    scout_duration = min(float(settings.scout_duration_sec), blind.start_sec,
                         duration_sec - blind.start_sec - blind.duration_sec)
    left_count = max(1, min(count - 1, round(count * blind.start_sec / (duration_sec - blind.duration_sec)))) if count > 1 else 1
    regions = ((0.0, blind.start_sec, left_count),
               (blind.start_sec + blind.duration_sec, duration_sec, count - left_count))
    starts: list[float] = []
    for low, high, region_count in regions:
        if not region_count:
            continue
        if high - low < scout_duration:
            continue
        cell = (high - low) / region_count
        for index in range(region_count):
            if cell >= scout_duration:
                start = low + index * cell + (cell - scout_duration) * _unit_hash(seed, f"scout:{len(starts)}")
            else:
                start = low + (high - low - scout_duration) * index / max(1, region_count - 1)
            starts.append(start)
    return tuple(
        ScoutWindow(id=f"scout-{index + 1:03d}", start_sec=start, duration_sec=scout_duration)
        for index, start in enumerate(starts)
    )


def _midrank_percentiles(values: list[float]) -> list[float]:
    if not values or any(not math.isfinite(value) for value in values):
        raise SamplePlanningError("Scout metrics must be finite and non-empty.")
    count = len(values)
    result = [0.0] * count
    by_value = sorted(range(count), key=lambda index: (values[index], index))
    position = 0
    while position < count:
        end = position + 1
        while end < count and values[by_value[end]] == values[by_value[position]]:
            end += 1
        # Midrank percentiles use [0, 1].  A singleton video is not planned
        # through this path, but keeping its value defined makes the helper sane.
        rank = 0.5 if count == 1 else ((position + end - 1) / 2.0) / (count - 1)
        for sorted_index in range(position, end):
            result[by_value[sorted_index]] = rank
        position = end
    return result


def rank_scout_observations(observations: Iterable[ScoutObservation]) -> tuple[RankedScoutObservation, ...]:
    items = tuple(observations)
    if not items:
        raise SamplePlanningError("Long videos require at least one scout observation.")
    ids = [item.window.id for item in items]
    if len(set(ids)) != len(ids):
        raise SamplePlanningError("Scout window IDs must be unique.")
    si_ranks = _midrank_percentiles([item.si_p90 for item in items])
    ti_ranks = _midrank_percentiles([item.ti_p90 for item in items])
    transition_ranks = _midrank_percentiles([item.max_scene_score for item in items])

    def component(raw: float, scale: float, rank: float) -> float:
        return 0.55 * min(1.0, max(0.0, raw / scale)) + 0.45 * rank

    result: list[RankedScoutObservation] = []
    for index, item in enumerate(items):
        risk = CompressionRiskVector(
            spatial_detail=component(item.si_p90, 100.0, si_ranks[index]),
            motion=component(item.ti_p90, 50.0, ti_ranks[index]),
            transition=component(item.max_scene_score, 100.0, transition_ranks[index]),
            dark=min(1.0, max(0.0, item.dark)),
            flat_or_gradient=min(1.0, max(0.0, item.flat_or_gradient)),
            high_frequency_or_noise=min(1.0, max(0.0, item.high_frequency_or_noise)),
            text_edge_like=min(1.0, max(0.0, item.text_edge_like)),
        )
        result.append(
            RankedScoutObservation(
                observation=item,
                si_rank=si_ranks[index],
                ti_rank=ti_ranks[index],
                difficulty=risk.global_risk,
                risk=risk,
            )
        )
    return tuple(result)


def ranked_scout_payloads(observations: Iterable[ScoutObservation]) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for ranked in rank_scout_observations(observations):
        payload = scout_observation_payload(ranked.observation)
        payload.update(
            {
                "si_rank": ranked.si_rank,
                "ti_rank": ranked.ti_rank,
                "difficulty": ranked.difficulty,
                "compression_risk": {
                    name: getattr(ranked.risk, name)
                    for name in ranked.risk.__dataclass_fields__
                },
            }
        )
        payloads.append(payload)
    return payloads


def content_statistics(
    observations: Iterable[ScoutObservation],
) -> tuple[float, float]:
    ranked = rank_scout_observations(observations)
    names = tuple(CompressionRiskVector.__dataclass_fields__)
    vectors = [tuple(getattr(item.risk, name) for name in names) for item in ranked]
    dispersion = (
        statistics.fmean(statistics.pstdev(values) for values in zip(*vectors))
        if len(vectors) > 1
        else 0.0
    )
    scene_density = sum(len(item.observation.scene_cut_times) for item in ranked) / max(1, len(ranked))
    absolute = statistics.fmean(item.risk.global_risk for item in ranked)
    high_modes = sum(
        max(getattr(item.risk, name) for item in ranked) >= 0.65
        for name in names[:-1]
    ) / max(1, len(names) - 1)
    heterogeneity = min(1.0, 2.2 * dispersion + 0.12 * scene_density + 0.25 * high_modes)
    uncertainty = min(1.0, 0.55 * heterogeneity + 0.30 * absolute + 0.15 / math.sqrt(len(ranked)))
    return uncertainty, heterogeneity


def adaptive_search_window_count(
    duration_sec: float,
    settings: AnalysisProfileSettings,
    observations: Iterable[ScoutObservation],
) -> int:
    uncertainty, heterogeneity = content_statistics(observations)
    duration_factor = min(1.0, math.log1p(duration_sec / 600.0) / math.log1p(18.0))
    span = settings.search_max_windows - settings.search_min_windows
    extra = round(span * (0.30 * duration_factor + 0.45 * uncertainty + 0.25 * heterogeneity))
    return min(settings.search_max_windows, settings.search_min_windows + max(0, extra))


def _project_window(
    observation: RankedScoutObservation,
    *,
    duration_sec: float,
    sample_duration_sec: float,
    kind: str,
    reasons: Iterable[str],
) -> PlannedWindow:
    sample_duration = min(sample_duration_sec, duration_sec)
    start = min(
        max(0.0, observation.observation.window.center_sec - sample_duration / 2.0),
        max(0.0, duration_sec - sample_duration),
    )
    return PlannedWindow(
        id=f"{kind}:{observation.observation.window.id}",
        start_sec=start,
        duration_sec=sample_duration,
        reasons=tuple(dict.fromkeys(reasons)),
        scout_id=observation.observation.window.id,
    )


def _overlaps(left: PlannedWindow, right: PlannedWindow) -> bool:
    return left.start_sec < right.start_sec + right.duration_sec - _EPSILON and right.start_sec < left.start_sec + left.duration_sec - _EPSILON


def _add_window(
    selected: list[PlannedWindow],
    candidate: PlannedWindow,
    *,
    limit: int,
    allow_merge: bool = True,
) -> bool:
    existing_index = next(
        (
            index
            for index, window in enumerate(selected)
            if window.scout_id == candidate.scout_id
        ),
        None,
    )
    if existing_index is not None:
        if not allow_merge:
            return False
        existing = selected[existing_index]
        selected[existing_index] = replace(
            existing, reasons=tuple(dict.fromkeys((*existing.reasons, *candidate.reasons)))
        )
        return True
    overlap_index = next(
        (index for index, window in enumerate(selected) if _overlaps(candidate, window)),
        None,
    )
    if overlap_index is not None:
        if not allow_merge:
            return False
        existing = selected[overlap_index]
        selected[overlap_index] = replace(
            existing,
            reasons=tuple(dict.fromkeys((*existing.reasons, *candidate.reasons))),
        )
        return True
    if len(selected) >= limit:
        return False
    selected.append(candidate)
    return True


def _available_window_capacity(
    ranked: tuple[RankedScoutObservation, ...],
    protected: list[PlannedWindow],
    *,
    duration_sec: float,
    sample_duration_sec: float,
) -> int:
    """Return the maximum remaining non-overlapping projected-window count."""

    used_scout_ids = {window.scout_id for window in protected}
    candidates = [
        _project_window(
            item,
            duration_sec=duration_sec,
            sample_duration_sec=sample_duration_sec,
            kind="capacity",
            reasons=(),
        )
        for item in ranked
        if item.observation.window.id not in used_scout_ids
    ]
    candidates = [
        candidate
        for candidate in candidates
        if not any(_overlaps(candidate, window) for window in protected)
    ]
    candidates.sort(
        key=lambda window: (
            window.start_sec + window.duration_sec,
            window.start_sec,
            window.id,
        )
    )
    selected: list[PlannedWindow] = []
    for candidate in candidates:
        if not selected or not _overlaps(candidate, selected[-1]):
            selected.append(candidate)
    return len(selected)


def _ranked_desc(
    items: Iterable[RankedScoutObservation],
    key: str,
    *,
    anchor_sec: float | None = None,
) -> list[RankedScoutObservation]:
    def tie_breaker(item: RankedScoutObservation) -> tuple[float, float, str]:
        distance = (
            0.0
            if anchor_sec is None
            else abs(item.observation.window.center_sec - anchor_sec)
        )
        return (distance, item.observation.window.start_sec, item.observation.window.id)

    if key == "si":
        return sorted(items, key=lambda item: (-item.si_rank, *tie_breaker(item)))
    if key == "ti":
        return sorted(items, key=lambda item: (-item.ti_rank, *tie_breaker(item)))
    return sorted(items, key=lambda item: (-item.difficulty, *tie_breaker(item)))


def _coverage_candidates(
    ranked: tuple[RankedScoutObservation, ...], bins: int, duration_sec: float
) -> list[tuple[list[RankedScoutObservation], str, float]]:
    choices: list[tuple[list[RankedScoutObservation], str, float]] = []
    for bin_index in range(bins):
        low = duration_sec * bin_index / bins
        high = duration_sec * (bin_index + 1) / bins
        within = [
            item
            for item in ranked
            if low <= item.observation.window.center_sec < high
            or (bin_index == bins - 1 and low <= item.observation.window.center_sec <= high)
        ]
        if within:
            midpoint = (low + high) / 2.0 if bins > 1 else duration_sec * 0.25
            choices.append(
                (
                    _ranked_desc(within, "difficulty", anchor_sec=midpoint),
                    f"coverage_bin_{bin_index + 1}",
                    midpoint,
                )
            )
    return choices


def _select_holdouts(
    ranked: tuple[RankedScoutObservation, ...],
    search: list[PlannedWindow],
    *,
    duration_sec: float,
    sample_duration_sec: float,
    count: int,
    capacity_reserve: int = 0,
) -> list[PlannedWindow]:
    holdouts: list[PlannedWindow] = []
    if count <= 0:
        return holdouts
    available = [
        item
        for item in _ranked_desc(ranked, "difficulty", anchor_sec=duration_sec / 2.0)
        if item.observation.window.id not in {window.scout_id for window in search}
    ]
    while available and len(holdouts) < count:
        anchors = [window.center_sec for window in (*search, *holdouts)]

        def score(
            item: RankedScoutObservation,
            occupied: tuple[float, ...] = tuple(anchors),
        ) -> tuple[float, float, float, str]:
            distance = min((abs(item.observation.window.center_sec - anchor) for anchor in occupied), default=duration_sec)
            diversity = min(1.0, distance / max(duration_sec / max(count + len(search), 1), 1.0))
            return (
                0.70 * item.difficulty + 0.30 * diversity,
                item.difficulty,
                -item.observation.window.start_sec,
                item.observation.window.id,
            )

        candidate_ranked = max(available, key=score)
        candidate = _project_window(
            candidate_ranked,
            duration_sec=duration_sec,
            sample_duration_sec=sample_duration_sec,
            kind="holdout",
            reasons=("holdout_difficulty_and_diversity",),
        )
        available.remove(candidate_ranked)
        if any(_overlaps(candidate, window) for window in (*search, *holdouts)):
            continue
        remaining = count - len(holdouts) - 1 + capacity_reserve
        if _available_window_capacity(
            ranked,
            [*search, *holdouts, candidate],
            duration_sec=duration_sec,
            sample_duration_sec=sample_duration_sec,
        ) < remaining:
            continue
        holdouts.append(candidate)
    return holdouts


def build_sample_plan(
    duration_sec: float,
    settings: AnalysisProfileSettings,
    observations: Iterable[ScoutObservation] = (),
    *, seed: str = "",
) -> SamplePlan:
    """Select hard and timeline-representative search/holdout windows."""

    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise SamplePlanningError("Source duration must be finite and positive.")
    if should_analyze_whole_video(duration_sec, settings):
        whole = PlannedWindow(
            id="search:whole-video", start_sec=0.0, duration_sec=duration_sec, reasons=("whole_video",)
        )
        return SamplePlan((), (whole,), (), True)

    ranked = rank_scout_observations(observations)
    expected = {window.id for window in plan_scout_windows(duration_sec, settings, seed=seed)}
    actual = {item.observation.window.id for item in ranked}
    if actual != expected:
        raise SamplePlanningError("Scout observations do not match the deterministic scout plan.")

    uncertainty, heterogeneity = content_statistics(item.observation for item in ranked)
    target_holdouts = settings.holdout_target_min + round(
        (settings.holdout_target_max - settings.holdout_target_min) * uncertainty
    )
    expected_holdouts = max(holdout_window_count(duration_sec, settings), target_holdouts)
    expected_holdouts = max(1, min(settings.holdout_target_max, expected_holdouts))
    blind = _blind_window(duration_sec, settings, seed)
    risk_holdouts = expected_holdouts - 1
    projected_capacity = _available_window_capacity(
        ranked,
        [blind],
        duration_sec=duration_sec,
        sample_duration_sec=settings.sample_duration_sec,
    )
    expected_reserves = min(
        settings.reserve_window_count,
        max(0, projected_capacity - risk_holdouts - 1),
    )
    target = max(
        1,
        min(
            adaptive_search_window_count(duration_sec, settings, (item.observation for item in ranked)),
            projected_capacity - risk_holdouts - expected_reserves,
        ),
    )
    if projected_capacity < risk_holdouts + 1:
        whole = PlannedWindow("search:whole-video", 0.0, duration_sec, ("whole_video", "insufficient_independent_capacity"))
        return SamplePlan(tuple(item.observation.window for item in ranked), (whole,), (), True)
    search: list[PlannedWindow] = []

    def add_search(candidate: PlannedWindow, *, allow_merge: bool = True) -> bool:
        if _overlaps(candidate, blind):
            return False
        trial = list(search)
        if not _add_window(
            trial,
            candidate,
            limit=target,
            allow_merge=allow_merge,
        ):
            return False
        if len(trial) > len(search):
            remaining = target - len(trial) + risk_holdouts + expected_reserves
            if _available_window_capacity(
                ranked,
                [blind, *trial],
                duration_sec=duration_sec,
                sample_duration_sec=settings.sample_duration_sec,
            ) < remaining:
                return False
        search[:] = trial
        return True

    # Coverage has an explicit budget; risk representatives use the remainder.
    coverage_slots = max(1, target // 2)
    for candidates, reason, _midpoint in _coverage_candidates(ranked, coverage_slots, duration_sec):
        for candidate_ranked in candidates:
            candidate = _project_window(candidate_ranked, duration_sec=duration_sec,
                                        sample_duration_sec=settings.sample_duration_sec,
                                        kind="search", reasons=(reason,))
            bin_index = int(reason.rsplit("_", 1)[1]) - 1
            if not bin_index * duration_sec / coverage_slots <= candidate.center_sec < (bin_index + 1) * duration_sec / coverage_slots:
                continue
            if add_search(candidate, allow_merge=False):
                break
        else:
            whole = PlannedWindow("search:whole-video", 0.0, duration_sec, ("whole_video", "insufficient_coverage_capacity"))
            return SamplePlan(tuple(item.observation.window for item in ranked), (whole,), (), True)
    representatives = (
        ("si", "highest_spatial_risk", 0.25),
        ("ti", "highest_motion_risk", 0.75),
        ("difficulty", "global_compression_risk", 0.50),
    )
    for key, reason, anchor_fraction in representatives:
        for ranked_candidate in _ranked_desc(ranked, key, anchor_sec=duration_sec * anchor_fraction):
            candidate = _project_window(
                ranked_candidate, duration_sec=duration_sec,
                sample_duration_sec=settings.sample_duration_sec, kind="search",
                reasons=(reason, {
                    "highest_spatial_risk": "highest_si",
                    "highest_motion_risk": "highest_ti",
                    "global_compression_risk": "global_hardest",
                }[reason]),
            )
            if add_search(candidate):
                break

    transition_candidates = sorted(
        ranked,
        key=lambda item: (
            -item.risk.transition,
            -item.observation.max_scene_score,
            item.observation.window.start_sec,
        ),
    )
    if transition_candidates and (
        transition_candidates[0].observation.max_scene_score >= 10.0
        or transition_candidates[0].observation.scene_cut_times
    ):
        add_search(
            _project_window(
                transition_candidates[0],
                duration_sec=duration_sec,
                sample_duration_sec=settings.sample_duration_sec,
                kind="search",
                reasons=("transition_risk",),
            ),
            allow_merge=True,
        )

    for candidate_ranked in _ranked_desc(
        ranked, "difficulty", anchor_sec=duration_sec / 2.0
    ):
        if len(search) >= target:
            break
        add_search(
            _project_window(
                candidate_ranked,
                duration_sec=duration_sec,
                sample_duration_sec=settings.sample_duration_sec,
                kind="search",
                reasons=("hardship",),
            ),
        )
    if len(search) != target:
        raise SamplePlanningError("Unable to select the requested number of non-overlapping search windows.")
    search.sort(key=lambda window: (window.start_sec, window.id))

    holdouts = _select_holdouts(
        ranked,
        [*search, blind],
        duration_sec=duration_sec,
        sample_duration_sec=settings.sample_duration_sec,
        count=risk_holdouts,
        capacity_reserve=expected_reserves,
    )
    holdouts.append(blind)
    if len(holdouts) != expected_holdouts:
        raise SamplePlanningError(
            "Unable to select the requested number of independent holdout windows."
        )
    holdouts.sort(key=lambda window: (window.start_sec, window.id))
    reserve_seed = _select_holdouts(
        ranked,
        [*search, *holdouts],
        duration_sec=duration_sec,
        sample_duration_sec=settings.sample_duration_sec,
        count=expected_reserves,
    )
    reserves = [
        replace(
            window,
            id=window.id.replace("holdout:", "reserve:", 1),
            reasons=("reserve_risk_and_timeline_diversity",),
        )
        for window in reserve_seed
    ]
    if len(reserves) != expected_reserves:
        raise SamplePlanningError("Unable to reserve fresh independent validation windows.")
    reserves.sort(key=lambda window: (window.start_sec, window.id))
    return SamplePlan(
        scout_windows=tuple(item.observation.window for item in ranked),
        search_windows=tuple(search),
        holdout_windows=tuple(holdouts),
        whole_video=False,
        reserve_windows=tuple(reserves),
        content_uncertainty=uncertainty,
        content_heterogeneity=heterogeneity,
    )


def align_window_to_scene_cuts(
    window: PlannedWindow,
    scene_cut_times: Iterable[float],
    source_duration_sec: float,
) -> PlannedWindow:
    """Move a window into a nearby shot where its duration allows it.

    No scene cut is treated as crossing when it lies exactly on a window edge.
    If no shot can contain the desired duration, the original window is kept
    and explicitly marked for receipt/log visibility.
    """

    if not math.isfinite(source_duration_sec) or source_duration_sec <= 0:
        raise SamplePlanningError("Source duration must be finite and positive.")
    cuts = sorted(
        {
            float(cut)
            for cut in scene_cut_times
            if math.isfinite(float(cut)) and _EPSILON < float(cut) < source_duration_sec - _EPSILON
        }
    )
    if "transition_risk" in window.reasons:
        crosses = any(
            window.start_sec + _EPSILON < cut < window.start_sec + window.duration_sec - _EPSILON
            for cut in cuts
        )
        return replace(window, crosses_scene_cut=crosses)
    end = window.start_sec + window.duration_sec
    if not any(window.start_sec + _EPSILON < cut < end - _EPSILON for cut in cuts):
        return replace(window, crosses_scene_cut=False)
    boundaries = [0.0, *cuts, source_duration_sec]
    viable: list[tuple[float, float]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        if right - left + _EPSILON >= window.duration_sec:
            viable.append((left, right))
    if not viable:
        return replace(window, crosses_scene_cut=True)
    desired_center = window.center_sec

    def placement(shot: tuple[float, float]) -> tuple[float, float]:
        left, right = shot
        start = min(max(window.start_sec, left), right - window.duration_sec)
        return start, abs((start + window.duration_sec / 2.0) - desired_center)

    start, _distance = min((placement(shot) for shot in viable), key=lambda item: (item[1], item[0]))
    return replace(window, start_sec=start, crosses_scene_cut=False)
