"""Execute Smart content scouting while keeping search orchestration separate."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Callable

from .complexity import (
    ComplexityProbeError,
    build_scene_guard_command,
    build_scout_command,
    parse_scene_guard_metadata,
    parse_scout_metadata,
)
from core.models import AnalysisProfileSettings
from .planner import (
    PlannedWindow,
    SamplePlan,
    ScoutObservation,
    align_window_to_scene_cuts,
    build_sample_plan,
    plan_scout_windows,
)


RunCommand = Callable[[list[str], str], None]
ProgressCallback = Callable[[str, dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class SamplingResult:
    plan: SamplePlan
    observations: tuple[ScoutObservation, ...]


def _read_metadata(path: Path, phase: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ComplexityProbeError(f"{phase} metadata could not be read: {exc}") from exc


def discover_sample_plan(
    *,
    ffmpeg_path: Path,
    source_path: Path,
    source_duration_sec: float,
    settings: AnalysisProfileSettings,
    temp_root: Path,
    run_command: RunCommand,
    progress: ProgressCallback,
) -> SamplingResult:
    stat = source_path.stat()
    seed = hashlib.sha256(f"{source_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
    scouts = plan_scout_windows(source_duration_sec, settings, seed=seed)
    if not scouts:
        whole = PlannedWindow("search:whole-video", 0.0, source_duration_sec, ("whole_video",))
        return SamplingResult(SamplePlan((), (whole,), (), True), ())

    observations: list[ScoutObservation] = []
    for index, scout in enumerate(scouts):
        progress(
            "scouting",
            {
                "scout_index": index + 1,
                "scout_count": len(scouts),
                "scout_start_sec": scout.start_sec,
            },
        )
        metadata_path = temp_root / f"scout-{index:03d}.txt"
        run_command(
            build_scout_command(
                ffmpeg_path,
                source_path,
                start_sec=scout.start_sec,
                duration_sec=scout.duration_sec,
                metadata_path=metadata_path,
            ),
            "content complexity scout",
        )
        metrics = parse_scout_metadata(_read_metadata(metadata_path, "Scout"))
        observations.append(
            ScoutObservation(
                window=scout,
                si_p90=metrics.si_p90,
                ti_p90=metrics.ti_p90,
                scene_cut_times=tuple(scout.start_sec + value for value in metrics.scene_cut_times),
                max_scene_score=metrics.max_scene_score,
                dark=max(0.0, min(1.0, (64.0 - metrics.luma_mean) / 64.0)),
                flat_or_gradient=max(0.0, 1.0 - min(1.0, metrics.si_p90 / 100.0)),
                high_frequency_or_noise=min(1.0, metrics.si_p90 / 100.0)
                * max(0.0, 1.0 - min(1.0, metrics.ti_p90 / 50.0)),
            )
        )
    progress("scout_finished", {"scout_count": len(observations)})
    plan = build_sample_plan(source_duration_sec, settings, observations, seed=seed)
    progress(
        "sample_plan_ready",
        {
            "search_window_count": len(plan.search_windows),
            "holdout_window_count": len(plan.holdout_windows),
            "reserve_window_count": len(plan.reserve_windows),
        },
    )
    return SamplingResult(
        _align_plan(
            plan,
            ffmpeg_path=ffmpeg_path,
            source_path=source_path,
            source_duration_sec=source_duration_sec,
            temp_root=temp_root,
            run_command=run_command,
            progress=progress,
        ),
        tuple(observations),
    )


def _align_plan(
    plan: SamplePlan,
    *,
    ffmpeg_path: Path,
    source_path: Path,
    source_duration_sec: float,
    temp_root: Path,
    run_command: RunCommand,
    progress: ProgressCallback,
) -> SamplePlan:
    if plan.whole_video:
        return plan
    aligned: list[PlannedWindow] = []
    all_windows = [*plan.search_windows, *plan.holdout_windows, *plan.reserve_windows]
    coverage_bins = max(1, len(plan.search_windows) // 2)
    for index, window in enumerate(all_windows):
        if "unscouted_validation" in window.reasons:
            aligned.append(window)
            continue
        progress(
            "boundary_alignment",
            {"window_index": index + 1, "window_count": len(all_windows)},
        )
        guard_start = max(0.0, window.start_sec - window.duration_sec)
        guard_end = min(source_duration_sec, window.start_sec + 2.0 * window.duration_sec)
        metadata_path = temp_root / f"scene-guard-{index:03d}.txt"
        run_command(
            build_scene_guard_command(
                ffmpeg_path,
                source_path,
                start_sec=guard_start,
                duration_sec=guard_end - guard_start,
                metadata_path=metadata_path,
            ),
            "scene boundary alignment",
        )
        cuts = parse_scene_guard_metadata(_read_metadata(metadata_path, "Scene-guard"))
        candidate = align_window_to_scene_cuts(
            window,
            (guard_start + value for value in cuts),
            source_duration_sec,
        )
        for reason in window.reasons:
            if reason.startswith("coverage_bin_"):
                bin_index = int(reason.rsplit("_", 1)[1]) - 1
                if not bin_index * source_duration_sec / coverage_bins <= candidate.center_sec < (bin_index + 1) * source_duration_sec / coverage_bins:
                    candidate = replace(window, crosses_scene_cut=True)
        original_peers = [*all_windows[:index], *all_windows[index + 1 :]]
        protected = [*aligned, *original_peers]
        overlaps = any(
            candidate.start_sec < existing.start_sec + existing.duration_sec - 1e-9
            and existing.start_sec < candidate.start_sec + candidate.duration_sec - 1e-9
            for existing in protected
        )
        aligned.append(replace(window, crosses_scene_cut=True) if overlaps else candidate)
    search_count = len(plan.search_windows)
    holdout_count = len(plan.holdout_windows)
    return replace(
        plan,
        search_windows=tuple(aligned[:search_count]),
        holdout_windows=tuple(aligned[search_count : search_count + holdout_count]),
        reserve_windows=tuple(aligned[search_count + holdout_count :]),
    )
