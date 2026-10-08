"""Smart sampling primitives and the FFmpeg Scout adapter."""

from .complexity import (
    ComplexityProbeError,
    SCENE_CHANGE_THRESHOLD,
    SCOUT_FPS,
    SCOUT_MAX_WIDTH,
    ScoutMetrics,
    build_scene_guard_command,
    build_scout_command,
    parse_scene_guard_metadata,
    parse_scout_metadata,
)
from .planner import (
    PlannedWindow,
    RankedScoutObservation,
    SamplePlan,
    SamplePlanningError,
    ScoutObservation,
    ScoutWindow,
    align_window_to_scene_cuts,
    build_sample_plan,
    holdout_window_count,
    plan_scout_windows,
    planned_window_from_payload,
    planned_window_payload,
    rank_scout_observations,
    ranked_scout_payloads,
    scout_observation_from_payload,
    scout_observation_payload,
    search_window_count,
    should_analyze_whole_video,
)
from .scout import SamplingResult, discover_sample_plan

__all__ = [
    "ComplexityProbeError", "PlannedWindow", "RankedScoutObservation",
    "SCENE_CHANGE_THRESHOLD", "SCOUT_FPS", "SCOUT_MAX_WIDTH", "SamplePlan",
    "SamplePlanningError", "SamplingResult", "ScoutMetrics", "ScoutObservation",
    "ScoutWindow", "align_window_to_scene_cuts", "build_sample_plan",
    "build_scene_guard_command", "build_scout_command", "discover_sample_plan",
    "holdout_window_count", "plan_scout_windows", "planned_window_from_payload",
    "planned_window_payload", "parse_scene_guard_metadata", "parse_scout_metadata",
    "rank_scout_observations", "ranked_scout_payloads", "scout_observation_from_payload",
    "scout_observation_payload", "search_window_count", "should_analyze_whole_video",
]
