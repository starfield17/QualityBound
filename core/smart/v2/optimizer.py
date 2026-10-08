"""Bounded discrete rate-quality allocation, independent of FFmpeg and files."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from core.models import AnalysisProfileName, ShotAnalysis, ShotCandidate, ShotRange


@dataclass(frozen=True, slots=True)
class V2Settings:
    search_windows: int
    window_sec: float
    candidate_limit: int
    state_limit: int
    refinement_limit: int


SETTINGS = {
    AnalysisProfileName.FAST: V2Settings(1, 2.0, 3, 2048, 1),
    AnalysisProfileName.BALANCE: V2Settings(2, 2.0, 5, 8192, 2),
    AnalysisProfileName.PRECISE: V2Settings(3, 3.0, 7, 16384, 3),
}


def sample_windows(shot: ShotRange, fps: float, settings: V2Settings) -> tuple[list[ShotRange], ShotRange | None]:
    length = max(1, math.ceil(fps * settings.window_sec))
    count = settings.search_windows + 1
    if shot.frame_count <= count * length:
        return [shot], None
    # One window per equal time stratum. The final stratum stays out of search.
    windows = []
    for index in range(count):
        left = shot.start_frame + shot.frame_count * index // count
        right = shot.start_frame + shot.frame_count * (index + 1) // count
        start = left + (right - left - length) // 2
        windows.append(ShotRange(start, start + length))
    return windows[:-1], windows[-1]


def worst_one_second(scores: Sequence[float], fps: float) -> float:
    if not scores:
        raise ValueError("Quality measurement must contain frames.")
    length = min(len(scores), max(1, math.ceil(fps)))
    rolling = sum(scores[:length])
    lowest = rolling
    for index in range(length, len(scores)):
        rolling += scores[index] - scores[index - length]
        lowest = min(lowest, rolling)
    return lowest / length


def pareto_candidates(candidates: list[ShotCandidate], target: float) -> list[ShotCandidate]:
    eligible = [c for c in candidates if c.mean_vmaf >= max(0.0, target - 4.0)
                and c.worst_1s_vmaf >= max(0.0, target - 8.0)]
    ordered = sorted(eligible, key=lambda c: (c.predicted_video_bytes, -c.mean_vmaf, c.bitrate_bps))
    frontier: list[ShotCandidate] = []
    quality = -math.inf
    for candidate in ordered:
        if candidate.mean_vmaf > quality:
            frontier.append(candidate)
            quality = candidate.mean_vmaf
    return frontier


@dataclass(frozen=True, slots=True)
class Allocation:
    selected: tuple[ShotCandidate, ...]
    video_bytes: int
    mean_vmaf: float
    approximate: bool


@dataclass(frozen=True, slots=True)
class _State:
    size: int
    quality_sum: float
    previous: _State | None
    candidate: ShotCandidate | None


def allocate(shots: list[ShotAnalysis], target: float, video_budget: int | None,
             state_limit: int, *, maximize_quality: bool = False,
             diagnostics: dict[str, bool] | None = None) -> Allocation | None:
    if not shots or state_limit < 2:
        raise ValueError("Allocation needs shots and at least two states.")
    states = [_State(0, 0.0, None, None)]
    approximate = False
    for analysis in shots:
        choices = pareto_candidates(analysis.candidates, target)
        if not choices:
            return None
        expanded = [_State(s.size + c.predicted_video_bytes,
                           s.quality_sum + c.mean_vmaf * analysis.shot.frame_count, s, c)
                    for s in states for c in choices
                    if video_budget is None or s.size + c.predicted_video_bytes <= video_budget]
        frontier: list[_State] = []
        best = -math.inf
        for state in sorted(expanded, key=lambda s: (s.size, -s.quality_sum)):
            if state.quality_sum > best:
                frontier.append(state)
                best = state.quality_sum
        if not frontier:
            return None
        if len(frontier) > state_limit:
            approximate = True
            if diagnostics is not None:
                diagnostics["approximate"] = True
            low, high = frontier[0].quality_sum, frontier[-1].quality_sum
            buckets: dict[int, _State] = {}
            for state in frontier:
                bucket = min(state_limit - 2, int((state.quality_sum - low) / (high - low) * (state_limit - 2)))
                old = buckets.get(bucket)
                if old is None or (state.size, -state.quality_sum) < (old.size, -old.quality_sum):
                    buckets[bucket] = state
            # Endpoints survive even if the top bucket kept a cheaper state.
            frontier = [*buckets.values(), frontier[-1]]
        states = frontier
    frames = sum(s.shot.frame_count for s in shots)
    eligible = states if maximize_quality else [s for s in states if s.quality_sum / frames >= target]
    if not eligible:
        return None
    chosen = (max(eligible, key=lambda s: (s.quality_sum, -s.size)) if maximize_quality
              else min(eligible, key=lambda s: (s.size, -s.quality_sum)))
    chain: list[ShotCandidate] = []
    cursor = chosen
    while cursor.candidate is not None:
        chain.append(cursor.candidate)
        assert cursor.previous is not None
        cursor = cursor.previous
    return Allocation(tuple(reversed(chain)), chosen.size, chosen.quality_sum / frames, approximate)
