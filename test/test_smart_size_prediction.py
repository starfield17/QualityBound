from __future__ import annotations

import unittest

from core.smart.v1.size_prediction import predict_size_distribution


class SmartSizePredictionTest(unittest.TestCase):
    def test_hardest_window_is_not_extrapolated_across_timeline(self) -> None:
        prediction = predict_size_distribution(
            requested_bitrate_bps=1_500_000,
            observed_window_bitrates=[2_000_000, 1_200_000, 1_150_000],
            duration_sec=600.0,
            audio_bitrate_bps=0,
            source_bytes=200_000_000,
            sample_risks=[1.0, 0.5, 0.2],
            timeline_risks=[0.2, 0.3, 0.4, 0.5, 0.6],
        )
        self.assertLess(prediction.mean_video_bitrate_bps, 1_500_000)
        self.assertLess(prediction.upper_video_bitrate_bps, 2_000_000)
        self.assertEqual(prediction.method, "risk_distribution_clipped_theil_sen")

    def test_uncertainty_is_bounded(self) -> None:
        prediction = predict_size_distribution(
            requested_bitrate_bps=1_000_000,
            observed_window_bitrates=[400_000, 2_500_000],
            duration_sec=60.0,
            audio_bitrate_bps=128_000,
            source_bytes=None,
        )
        self.assertGreaterEqual(prediction.uncertainty, 0.05)
        self.assertLessEqual(prediction.uncertainty, 0.30)


if __name__ == "__main__":
    unittest.main()
