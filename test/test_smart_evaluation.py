from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from core.smart.v1.evaluation import (
    EvaluationDataError,
    aggregate_evaluation_records,
    analysis_cost,
    bitrate_regret,
    calculate_case_metrics,
    is_false_size_block,
    is_quality_false_pass,
    load_evaluation_manifest,
    metrics_payload,
)
from scripts.evaluate_smart import main


def _measurement(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "analysis_wall_seconds": 20.0,
        "final_encode_wall_seconds": 100.0,
        "smart_passed": True,
        "ground_truth_passed": False,
        "smart_size_blocked": False,
        "full_encode_output_bytes": 900,
        "max_output_bytes": 1_000,
        "selected_video_bitrate_bps": 1_300_000,
        "oracle_minimum_bitrate_bps": 1_200_000,
        "counts": {
            "scout_windows": 12,
            "quality_encodes": 7,
            "vmaf_measurements": 7,
            "holdout_measurements": 1,
            "size_calibration_encodes": 0,
        },
    }
    data.update(overrides)
    return data


class SmartEvaluationMetricTest(unittest.TestCase):
    def test_core_metric_definitions(self) -> None:
        self.assertEqual(analysis_cost(20.0, 100.0), 0.2)
        self.assertTrue(is_quality_false_pass(smart_passed=True, ground_truth_passed=False))
        self.assertFalse(is_quality_false_pass(smart_passed=False, ground_truth_passed=False))
        self.assertTrue(
            is_false_size_block(
                smart_size_blocked=True,
                full_encode_output_bytes=900,
                max_output_bytes=1_000,
            )
        )
        regret = bitrate_regret(1_300_000, 1_200_000)
        self.assertEqual(regret.absolute_bps, 100_000)
        self.assertAlmostEqual(regret.normalized, 1 / 12)

    def test_missing_observations_are_not_added_to_denominators(self) -> None:
        metrics = calculate_case_metrics(
            _measurement(
                final_encode_wall_seconds=None,
                ground_truth_passed=None,
                full_encode_output_bytes=None,
                selected_video_bitrate_bps=None,
                oracle_minimum_bitrate_bps=None,
            )
        )
        self.assertIsNone(metrics.analysis_cost)
        self.assertIsNone(metrics.quality_false_pass)
        self.assertIsNone(metrics.false_size_block)
        self.assertIsNone(metrics.bitrate_regret_absolute_bps)

    def test_invalid_timings_and_bitrates_fail_closed(self) -> None:
        with self.assertRaises(EvaluationDataError):
            analysis_cost(1.0, 0.0)
        with self.assertRaises(EvaluationDataError):
            bitrate_regret(0, 1_000_000)

    def test_aggregate_rates_counts_and_regret(self) -> None:
        first = metrics_payload(calculate_case_metrics(_measurement()))
        second = metrics_payload(
            calculate_case_metrics(
                _measurement(
                    analysis_wall_seconds=10.0,
                    ground_truth_passed=True,
                    smart_size_blocked=True,
                    full_encode_output_bytes=800,
                    selected_video_bitrate_bps=1_200_000,
                )
            )
        )
        summary = aggregate_evaluation_records(
            [
                {"metrics": first, "counts": _measurement()["counts"], "error": None},
                {"metrics": second, "counts": _measurement()["counts"], "error": None},
                {"metrics": {}, "counts": {}, "error": "runner failed"},
            ]
        )
        self.assertEqual(summary["quality_false_pass_count"], 1)
        self.assertEqual(summary["quality_false_pass_denominator"], 2)
        self.assertEqual(summary["quality_false_pass_rate"], 0.5)
        self.assertEqual(summary["false_size_block_count"], 1)
        self.assertEqual(summary["false_size_block_denominator"], 1)
        self.assertEqual(summary["bitrate_regret_denominator"], 2)
        self.assertEqual(summary["totals"]["scout_windows"], 24)  # type: ignore[index]


class SmartEvaluationManifestTest(unittest.TestCase):
    def test_manifest_resolves_sources_and_enforces_one_input_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = root / "corpus.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "id": "clip-a",
                                "source": "media/a.mp4",
                                "command": ["runner", "--source", "{source}", "--output", "{result_path}"],
                                "metadata": {"category": "motion"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            case = load_evaluation_manifest(manifest)[0]
            self.assertEqual(case.source_path, (root / "media/a.mp4").resolve())
            self.assertEqual(case.metadata["category"], "motion")

            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["measurement"] = _measurement()
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(EvaluationDataError):
                load_evaluation_manifest(manifest)

    def test_inline_manifest_writes_ndjson_summary_and_csv_without_running_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-placeholder")
            ffmpeg = root / "ffmpeg"
            ffprobe = root / "ffprobe"
            ffmpeg.write_bytes(b"tool")
            ffprobe.write_bytes(b"tool")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "id": "inline",
                                "source": str(source),
                                "measurement": _measurement(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "results"
            exit_code = main(
                [
                    str(manifest),
                    "--ffmpeg",
                    str(ffmpeg),
                    "--ffprobe",
                    str(ffprobe),
                    "--output-dir",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(len((output / "results.ndjson").read_text(encoding="utf-8").splitlines()), 1)
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["case_count"], 1)
            self.assertEqual(summary["quality_false_pass_rate"], 1.0)
            self.assertIn("bitrate_regret_normalized", (output / "results.csv").read_text(encoding="utf-8"))

    def test_command_case_receives_result_path_and_is_executed_without_a_shell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mp4"
            source.write_bytes(b"synthetic-placeholder")
            ffmpeg = root / "ffmpeg"
            ffprobe = root / "ffprobe"
            ffmpeg.write_bytes(b"tool")
            ffprobe.write_bytes(b"tool")
            writer = (
                "from pathlib import Path; import json; "
                "Path(r'{result_path}').write_text(json.dumps(dict("
                "analysis_wall_seconds=2.0,final_encode_wall_seconds=10.0,"
                "smart_passed=True,ground_truth_passed=True,smart_size_blocked=False,"
                "full_encode_output_bytes=900,max_output_bytes=1000,"
                "selected_video_bitrate_bps=1200000,oracle_minimum_bitrate_bps=1200000,"
                "counts=dict(scout_windows=4,quality_encodes=3,vmaf_measurements=3,"
                "holdout_measurements=1,size_calibration_encodes=0))),encoding='utf-8')"
            )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cases": [
                            {
                                "id": "command",
                                "source": str(source),
                                "command": [sys.executable, "-c", writer],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = root / "results"
            self.assertEqual(
                main(
                    [
                        str(manifest),
                        "--ffmpeg",
                        str(ffmpeg),
                        "--ffprobe",
                        str(ffprobe),
                        "--output-dir",
                        str(output),
                    ]
                ),
                0,
            )
            record = json.loads((output / "results.ndjson").read_text(encoding="utf-8"))
            self.assertEqual(record["metrics"]["analysis_cost"], 0.2)
            self.assertEqual(record["counts"]["holdout_measurements"], 1)


if __name__ == "__main__":
    unittest.main()
