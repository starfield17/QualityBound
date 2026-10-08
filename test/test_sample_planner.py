from __future__ import annotations

import unittest

from core.smart.v1.profiles import FACTORY_ANALYSIS_PROFILES
from core.models import AnalysisProfileName, AnalysisProfileSettings
from core.smart.v1.sampling.planner import (
    PlannedWindow,
    ScoutObservation,
    align_window_to_scene_cuts,
    build_sample_plan,
    content_statistics,
    fresh_validation_window,
    holdout_window_count,
    plan_scout_windows,
    rank_scout_observations,
    search_window_count,
    should_analyze_whole_video,
)


def _observations(duration: float, settings: AnalysisProfileSettings) -> tuple[ScoutObservation, ...]:
    windows = plan_scout_windows(duration, settings)
    return tuple(
        ScoutObservation(window, si_p90=float(index % 5), ti_p90=float((index * 3) % 7))
        for index, window in enumerate(windows)
    )


class SamplePlannerTest(unittest.TestCase):
    def test_fresh_validation_uses_largest_gap_and_never_reuses_occupied_time(self) -> None:
        occupied = [(0.0, 15.0), (30.0, 20.0), (60.0, 5.0)]
        window = fresh_validation_window(100, 5, occupied, identity="holdout:fresh")
        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual((window.start_sec, window.duration_sec), (80.0, 5.0))
        self.assertIsNone(fresh_validation_window(10, 5, [(0, 6), (8, 2)], identity="holdout:none"))

    def test_capacity_across_seeds_and_short_durations(self) -> None:
        for name, settings in FACTORY_ANALYSIS_PROFILES.items():
            for duration in (settings.whole_video_max_sec + 0.1, 40.0, 60.0, 120.0):
                for seed in range(20):
                    with self.subTest(profile=name, duration=duration, seed=seed):
                        scouts = plan_scout_windows(duration, settings, seed=str(seed))
                        observations = [ScoutObservation(w, (i * 17) % 130, (i * 13) % 70)
                                        for i, w in enumerate(scouts)]
                        plan = build_sample_plan(duration, settings, observations, seed=str(seed))
                        windows = sorted((*plan.search_windows, *plan.holdout_windows, *plan.reserve_windows),
                                         key=lambda w: w.start_sec)
                        self.assertTrue(windows)
                        for left, right in zip(windows, windows[1:]):
                            self.assertLessEqual(left.start_sec + left.duration_sec, right.start_sec + 1e-9)
                        if not plan.whole_video:
                            self.assertTrue(any("unscouted_validation" in w.reasons for w in plan.holdout_windows))

    def test_profile_duration_buckets(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE]
        self.assertEqual(search_window_count(9 * 60, settings), 4)
        self.assertEqual(search_window_count(10 * 60, settings), 5)
        self.assertEqual(search_window_count(60 * 60, settings), 6)
        self.assertEqual(search_window_count(180 * 60, settings), 6)

    def test_precise_uses_over_180_minute_bucket_only_above_boundary(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.PRECISE]
        expected = (
            (180 * 60 - 0.001, 8, 3),
            (180 * 60, 8, 3),
            (180 * 60 + 0.001, 10, 4),
        )
        for duration, search_count, holdout_count in expected:
            with self.subTest(duration=duration):
                self.assertEqual(search_window_count(duration, settings), search_count)
                self.assertEqual(holdout_window_count(duration, settings), holdout_count)

    def test_scouts_are_stratified_and_non_overlapping(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE]
        windows = plan_scout_windows(2 * 60 * 60, settings)
        self.assertEqual(len(windows), 24)
        for left, right in zip(windows, windows[1:]):
            self.assertLessEqual(left.start_sec + left.duration_sec, right.start_sec)
        self.assertEqual(windows, plan_scout_windows(7200, settings))
        gaps = {round(right.start_sec - left.start_sec, 5) for left, right in zip(windows, windows[1:])}
        self.assertGreater(len(gaps), 1)

    def test_seeded_plans_keep_validation_outside_scout_and_coverage_honest(self) -> None:
        for profile in AnalysisProfileName:
            settings = FACTORY_ANALYSIS_PROFILES[profile]
            for seed in ("source-a", "source-b", "source-c"):
                scouts = plan_scout_windows(600, settings, seed=seed)
                observations = tuple(ScoutObservation(w, 100 if i < 5 else 1, 50 if i < 5 else 1)
                                     for i, w in enumerate(scouts))
                plan = build_sample_plan(600, settings, observations, seed=seed)
                self.assertFalse(plan.whole_video)
                independent = [w for w in plan.holdout_windows if "unscouted_validation" in w.reasons]
                self.assertEqual(len(independent), 1)
                for scout in scouts:
                    self.assertTrue(independent[0].start_sec + independent[0].duration_sec <= scout.start_sec + 1e-9
                                    or scout.start_sec + scout.duration_sec <= independent[0].start_sec + 1e-9)
                bins = max(1, len(plan.search_windows) // 2)
                for index in range(bins):
                    covered = [w for w in plan.search_windows if f"coverage_bin_{index + 1}" in w.reasons]
                    self.assertTrue(covered)
                    self.assertTrue(all(index * 600 / bins <= w.center_sec < (index + 1) * 600 / bins for w in covered))
                all_windows = sorted((*plan.search_windows, *plan.holdout_windows, *plan.reserve_windows), key=lambda w: w.start_sec)
                for left, right in zip(all_windows, all_windows[1:]):
                    self.assertLessEqual(left.start_sec + left.duration_sec, right.start_sec + 1e-9)
        self.assertNotEqual(plan_scout_windows(600, settings, seed="a"), plan_scout_windows(600, settings, seed="b"))

    def test_midrank_and_plan_reasons_are_deterministic(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE]
        observations = _observations(2 * 60 * 60, settings)
        ranked = rank_scout_observations(observations)
        self.assertEqual(len(ranked), len(observations))
        self.assertTrue(all(0.0 <= item.si_rank <= 1.0 for item in ranked))
        self.assertTrue(all(0.0 <= item.ti_rank <= 1.0 for item in ranked))
        tied = rank_scout_observations(
            (
                ScoutObservation(plan_scout_windows(120.0, settings)[0], 1.0, 2.0),
                ScoutObservation(plan_scout_windows(120.0, settings)[1], 1.0, 4.0),
            )
        )
        self.assertEqual((tied[0].si_rank, tied[1].si_rank), (0.5, 0.5))
        plan = build_sample_plan(2 * 60 * 60, settings, observations)
        self.assertGreaterEqual(len(plan.search_windows), settings.search_min_windows)
        self.assertLessEqual(len(plan.search_windows), settings.search_max_windows)
        self.assertEqual(len(plan.holdout_windows), 2)
        self.assertEqual(len(plan.reserve_windows), settings.reserve_window_count)
        reasons = {reason for window in plan.search_windows for reason in window.reasons}
        risk_reasons = {"highest_si", "highest_ti", "global_hardest", "hardship", "transition_risk"}
        self.assertTrue(reasons & risk_reasons)
        self.assertEqual(len(plan.search_windows), len({w.scout_id for w in plan.search_windows}))
        self.assertTrue(any("unscouted_validation" in w.reasons for w in plan.holdout_windows))
        self.assertTrue(any(reason.startswith("coverage_bin_") for reason in reasons))
        all_windows = (*plan.search_windows, *plan.holdout_windows, *plan.reserve_windows)
        self.assertEqual(len({window.id for window in all_windows}), len(all_windows))
        for left, right in zip(sorted(all_windows, key=lambda item: item.start_sec), sorted(all_windows, key=lambda item: item.start_sec)[1:]):
            self.assertLessEqual(left.start_sec + left.duration_sec, right.start_sec)

    def test_short_video_is_one_whole_search_window(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE]
        plan = build_sample_plan(20.0, settings)
        self.assertTrue(plan.whole_video)
        self.assertEqual(len(plan.search_windows), 1)
        self.assertFalse(plan.holdout_windows)
        self.assertEqual(plan.search_windows[0].reasons, ("whole_video",))

    def test_near_threshold_video_preserves_holdouts_without_planner_failure(
        self,
    ) -> None:
        thresholds = {
            AnalysisProfileName.FAST: 12.1,
            AnalysisProfileName.BALANCE: 20.1,
            AnalysisProfileName.PRECISE: 30.1,
        }
        for name, duration in thresholds.items():
            with self.subTest(profile=name.value):
                settings = FACTORY_ANALYSIS_PROFILES[name]
                self.assertFalse(should_analyze_whole_video(duration, settings))
                scouts = plan_scout_windows(duration, settings)
                observations = tuple(
                    ScoutObservation(window, si_p90=1.0, ti_p90=1.0)
                    for window in scouts
                )
                plan = build_sample_plan(duration, settings, observations)
                if plan.whole_video:
                    self.assertEqual(plan.search_windows[0].start_sec, 0.0)
                    self.assertEqual(plan.search_windows[0].duration_sec, duration)
                    self.assertEqual(plan.holdout_windows, ())
                    continue
                self.assertTrue(any("unscouted_validation" in w.reasons for w in plan.holdout_windows))
                self.assertGreaterEqual(len(plan.search_windows), 1)
                self.assertEqual(
                    len(plan.holdout_windows), settings.holdout_window_count
                )

    def test_equal_complexity_metrics_keep_coverage_and_holdout_budget(self) -> None:
        duration = 120.0
        for name in AnalysisProfileName:
            with self.subTest(profile=name.value):
                settings = FACTORY_ANALYSIS_PROFILES[name]
                observations = tuple(
                    ScoutObservation(window, si_p90=1.0, ti_p90=1.0)
                    for window in plan_scout_windows(duration, settings)
                )
                plan = build_sample_plan(duration, settings, observations)
                self.assertEqual(len(plan.search_windows), settings.search_min_windows)
                self.assertEqual(
                    len(plan.holdout_windows), settings.holdout_window_count
                )
                self.assertLess(
                    min(window.center_sec for window in plan.search_windows),
                    duration * 0.4,
                )
                self.assertGreater(
                    max(window.center_sec for window in plan.search_windows),
                    duration * 0.6,
                )
                all_windows = (*plan.search_windows, *plan.holdout_windows, *plan.reserve_windows)
                ordered = sorted(all_windows, key=lambda item: item.start_sec)
                for left, right in zip(ordered, ordered[1:]):
                    self.assertLessEqual(
                        left.start_sec + left.duration_sec, right.start_sec
                    )

    def test_absolute_risk_changes_uncertainty_with_identical_ranks(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE]
        windows = plan_scout_windows(600.0, settings)
        easy = tuple(
            ScoutObservation(window, si_p90=2.0 + index / 100, ti_p90=1.0 + index / 100)
            for index, window in enumerate(windows)
        )
        hard = tuple(
            ScoutObservation(window, si_p90=80.0 + index, ti_p90=35.0 + index)
            for index, window in enumerate(windows)
        )
        easy_uncertainty, _ = content_statistics(easy)
        hard_uncertainty, _ = content_statistics(hard)
        self.assertGreater(hard_uncertainty, easy_uncertainty)

    def test_transition_risk_is_selected_and_not_aligned_away(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE]
        windows = plan_scout_windows(600.0, settings)
        observations = tuple(
            ScoutObservation(
                window,
                si_p90=10.0,
                ti_p90=5.0,
                scene_cut_times=(window.center_sec,) if index == len(windows) // 2 else (),
                max_scene_score=80.0 if index == len(windows) // 2 else 0.0,
            )
            for index, window in enumerate(windows)
        )
        plan = build_sample_plan(600.0, settings, observations)
        transition = next(
            window for window in plan.search_windows if "transition_risk" in window.reasons
        )
        cut = next(item for item in observations if item.window.id == transition.scout_id).scene_cut_times[0]
        aligned = align_window_to_scene_cuts(transition, (cut,), 600.0)
        self.assertEqual(aligned.start_sec, transition.start_sec)
        self.assertTrue(aligned.crosses_scene_cut)

    def test_adaptive_workload_expands_for_heterogeneous_content(self) -> None:
        settings = FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE]
        windows = plan_scout_windows(3 * 60 * 60, settings)
        simple = tuple(
            ScoutObservation(window, si_p90=3.0, ti_p90=1.0)
            for window in windows
        )
        varied = tuple(
            ScoutObservation(
                window,
                si_p90=5.0 if index % 2 == 0 else 120.0,
                ti_p90=2.0 if index % 3 == 0 else 70.0,
                scene_cut_times=(window.center_sec,),
                max_scene_score=90.0 if index % 4 == 0 else 15.0,
                dark=1.0 if index % 5 == 0 else 0.0,
            )
            for index, window in enumerate(windows)
        )
        simple_plan = build_sample_plan(3 * 60 * 60, settings, simple)
        varied_plan = build_sample_plan(3 * 60 * 60, settings, varied)
        self.assertEqual(len(simple_plan.search_windows), settings.search_min_windows + 1)
        self.assertGreater(len(varied_plan.search_windows), len(simple_plan.search_windows))
        self.assertLessEqual(len(varied_plan.search_windows), settings.search_max_windows)

    def test_scene_alignment_avoids_cut_when_a_shot_can_fit(self) -> None:
        window = PlannedWindow("search:1", 8.0, 5.0, ("coverage",))
        aligned = align_window_to_scene_cuts(window, (10.0, 20.0), 30.0)
        self.assertFalse(aligned.crosses_scene_cut)
        self.assertFalse(aligned.start_sec < 10.0 < aligned.start_sec + aligned.duration_sec)

    def test_scene_alignment_records_unavoidable_crossing(self) -> None:
        window = PlannedWindow("search:1", 2.0, 8.0, ("coverage",))
        aligned = align_window_to_scene_cuts(window, (3.0, 6.0, 9.0), 12.0)
        self.assertTrue(aligned.crosses_scene_cut)
        self.assertEqual(aligned.start_sec, 2.0)


if __name__ == "__main__":
    unittest.main()
