from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from core.models import EncodeOptions, QualityCandidateResult, QualitySearchStatus
from core.smart.v1.bitrate import reselect_from_candidates, search_bitrate_candidates
from core.smart.v1.runtime import COARSE_MAX_CANDIDATES, EXACT_MAX_CANDIDATES, search_tolerance_bps

from _smart_quality_fixtures import _item


class SmartSearchTestCase(unittest.TestCase):
    def test_preferred_margin_is_soft_and_never_redefines_user_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            with source.open("wb") as fh:
                fh.truncate(100_000_000)
            item = _item(
                source,
                root / "out.mp4",
                EncodeOptions(min_vmaf=90.0, max_output_ratio=1.0),
            )
            candidates = [
                QualityCandidateResult(
                    video_bitrate_bps=1_600_000,
                    min_vmaf=90.2,
                    observed_video_bitrate_bps=1_600_000,
                ),
                QualityCandidateResult(
                    video_bitrate_bps=1_700_000,
                    min_vmaf=90.7,
                    observed_video_bitrate_bps=1_700_000,
                ),
            ]
            preferred = reselect_from_candidates(candidates, item)
            self.assertEqual(preferred.selected_video_bitrate_bps, 1_700_000)

            item.options = replace(item.options, max_output_ratio=0.155)
            fallback = reselect_from_candidates(candidates, item)
            self.assertEqual(fallback.status, QualitySearchStatus.FOUND)
            self.assertEqual(fallback.selected_video_bitrate_bps, 1_600_000)

    def test_search_selects_lowest_tested_passing_candidate(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 1_800_000 else 94.0,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
        )
        self.assertEqual(selected, min(item.video_bitrate_bps for item in candidates if item.min_vmaf >= 95.0))
        self.assertEqual(required, selected)
        self.assertLessEqual(len(tested), 8)
        self.assertEqual(len(tested), len(set(tested)))

    def test_non_monotonic_scores_still_choose_lowest_tested_pass(self) -> None:
        scores = {
            3_000_000: 96.0,
            500_000: 90.0,
            1_750_000: 95.5,
            1_125_000: 94.0,
            1_437_000: 95.2,
            1_281_000: 93.0,
        }

        def evaluate(bitrate: int) -> QualityCandidateResult:
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=scores.get(bitrate, 94.0))

        candidates, selected, _required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
        )
        passing = [candidate.video_bitrate_bps for candidate in candidates if candidate.min_vmaf >= 95.0]
        self.assertEqual(selected, min(passing))

    def test_failed_budget_candidate_estimates_required_bitrate_without_selecting_it(self) -> None:
        def evaluate(bitrate: int) -> QualityCandidateResult:
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 3_000_000 else 93.0,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=2_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
        )
        self.assertIsNone(selected)
        self.assertIsNotNone(required)
        self.assertGreater(required or 0, 2_000_000)
        self.assertIn(required, [candidate.video_bitrate_bps for candidate in candidates])

    def test_size_constraint_uses_measured_prediction_for_selection(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 1_800_000 else 94.0,
                predicted_output_bytes=bitrate // 1_000,
                predicted_output_ratio=bitrate / 10_000_000,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
            max_output_bytes=2_000,
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected, min(candidate.video_bitrate_bps for candidate in candidates if candidate.min_vmaf >= 95.0 and (candidate.predicted_output_bytes or 0) <= 2_000))
        self.assertEqual(required, min(candidate.video_bitrate_bps for candidate in candidates if candidate.min_vmaf >= 95.0))
        self.assertLessEqual(len(tested), 8)

    def test_size_constraint_rejects_quality_passing_candidates(self) -> None:
        def evaluate(bitrate: int) -> QualityCandidateResult:
            return QualityCandidateResult(
                video_bitrate_bps=bitrate,
                min_vmaf=96.0 if bitrate >= 1_800_000 else 94.0,
                predicted_output_bytes=10_000,
                predicted_output_ratio=1.0,
            )

        candidates, selected, required = search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=3_000_000,
            required_search_ceiling_bps=4_000_000,
            min_vmaf=95.0,
            max_output_bytes=2_000,
        )
        self.assertIsNone(selected)
        self.assertIsNotNone(required)
        required_candidate = next(candidate for candidate in candidates if candidate.video_bitrate_bps == required)
        self.assertEqual(required_candidate.predicted_output_ratio, 1.0)

    def test_search_never_tests_more_than_eight_unique_candidates(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=94.0)

        search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=1_000,
            budget_bitrate_bps=10_000_000,
            required_search_ceiling_bps=20_000_000,
            min_vmaf=95.0,
            max_output_bytes=1,
        )
        self.assertLessEqual(len(tested), 8)
        self.assertEqual(len(tested), len(set(tested)))

    def test_coarse_search_stops_at_four_candidates_and_wide_tolerance(self) -> None:
        tested: list[int] = []

        def evaluate(bitrate: int) -> QualityCandidateResult:
            tested.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=94.0)

        from core.smart.v1.runtime import COARSE_MAX_CANDIDATES, search_tolerance_bps

        search_bitrate_candidates(
            evaluate=evaluate,
            min_bitrate_bps=1_000,
            budget_bitrate_bps=10_000_000,
            required_search_ceiling_bps=20_000_000,
            min_vmaf=95.0,
            max_candidates=COARSE_MAX_CANDIDATES,
            max_output_bytes=1,
            tolerance_bps=search_tolerance_bps(20_000_000),
        )
        self.assertLessEqual(len(tested), 4)
        self.assertGreaterEqual(search_tolerance_bps(1_000_000), 50_000)
        self.assertEqual(search_tolerance_bps(10_000_000), 300_000)


class SmartExactSearchTestCase(unittest.TestCase):
    def test_selected_bitrate_cannot_come_from_coarse_candidates(self) -> None:
        coarse_only: list[int] = []
        exact_only: list[int] = []

        def coarse(bitrate: int) -> QualityCandidateResult:
            coarse_only.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=96.0, predicted_output_bytes=100)

        def exact(bitrate: int) -> QualityCandidateResult:
            exact_only.append(bitrate)
            return QualityCandidateResult(video_bitrate_bps=bitrate, min_vmaf=96.0, predicted_output_bytes=100)

        coarse_candidates, coarse_selected, _ = search_bitrate_candidates(
            evaluate=coarse,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=2_000_000,
            required_search_ceiling_bps=3_000_000,
            min_vmaf=95.0,
            max_candidates=COARSE_MAX_CANDIDATES,
            max_output_bytes=1_000,
            tolerance_bps=search_tolerance_bps(3_000_000),
        )
        exact_candidates, exact_selected, _ = search_bitrate_candidates(
            evaluate=exact,
            min_bitrate_bps=500_000,
            budget_bitrate_bps=coarse_selected or 2_000_000,
            required_search_ceiling_bps=3_000_000,
            min_vmaf=95.0,
            max_candidates=EXACT_MAX_CANDIDATES,
            max_output_bytes=1_000,
            tolerance_bps=search_tolerance_bps(3_000_000),
        )
        self.assertTrue(coarse_candidates)
        self.assertTrue(exact_candidates)
        self.assertIsNotNone(exact_selected)
        self.assertIn(exact_selected, exact_only)
        self.assertLessEqual(len(coarse_only), 4)
        self.assertLessEqual(len(exact_only), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
