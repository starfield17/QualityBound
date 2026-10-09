from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.smart.v1.measurement import score_candidate
from core.smart.v1.runtime import AnalysisTier
from core.smart.v1.vmaf import VmafWindowScore
import test_analysis_runtime as runtime_tests
from core.models import OperationCancelledError


class WindowMeasurementCacheTest(unittest.TestCase):
    def test_failure_and_cancellation_do_not_publish_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = runtime_tests.AnalysisSessionTestCase()._session(root)
            reference = root / "reference.mkv"
            reference.write_bytes(b"reference")
            for failure in (RuntimeError("invalid score"), OperationCancelledError("cancelled")):
                with self.subTest(failure=type(failure).__name__):
                    cache = {}
                    with patch("core.smart.v1.measurement.run_logged", side_effect=failure):
                        with self.assertRaises(type(failure)):
                            score_candidate(session.ffmpeg_path, session.item, [reference], 900_000,
                                            root, root, io.StringIO(), cancel_check=None,
                                            process_callback=None, plan=session.exact_plan,
                                            measurement_cache=cache, window_durations_sec=[5.0])
                    self.assertEqual(cache, {})

    def test_reuse_expansion_parameter_change_and_forced_remeasurement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = runtime_tests.AnalysisSessionTestCase()._session(root)
            references = [root / f"source-{index}.mkv" for index in range(3)]
            for reference in references:
                reference.write_bytes(b"reference")
            cache = {}
            calls: list[str] = []

            def run(command, *_args, **kwargs):
                calls.append(kwargs["phase"])
                if kwargs["phase"] == "candidate encode":
                    Path(command[-1]).write_bytes(b"encoded" * 100)

            def measure(refs, **overrides):
                options = dict(
                    cancel_check=None, process_callback=None,
                    window_durations_sec=[5.0] * len(refs), plan=session.exact_plan,
                    measurement_cache=cache,
                )
                options.update(overrides)
                return score_candidate(session.ffmpeg_path, session.item, refs, 900_000,
                                       root, root, io.StringIO(), **options)

            with patch("core.smart.v1.measurement.run_logged", side_effect=run), patch(
                "core.smart.v1.measurement.parse_vmaf_json",
                return_value=VmafWindowScore(96, 94, 93, 96),
            ):
                first = measure(references[:2])
                count = len(calls)
                self.assertEqual(measure(references[:2]), first)
                self.assertEqual(len(calls), count)
                expanded = measure(references)
                self.assertEqual(expanded.segment_vmaf, [96, 96, 96])
                self.assertEqual(calls.count("VMAF scoring"), 3)
                measure(references, force_remeasure=True)
                self.assertEqual(calls.count("VMAF scoring"), 6)
                measure(references, plan=replace(session.exact_plan, tier=AnalysisTier.COARSE, vmaf_subsample=3))
                self.assertEqual(calls.count("VMAF scoring"), 9)

    # F3 ← S1
    def test_partial_rejection_is_cached_by_window_not_list_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = runtime_tests.AnalysisSessionTestCase()._session(root)
            references = [root / f"ref-{index}.mkv" for index in range(3)]
            for reference in references:
                reference.write_bytes(b"reference")
            cache = {}

            def run(command, *_args, **kwargs):
                if kwargs["phase"] == "candidate encode":
                    Path(command[-1]).write_bytes(b"encoded")

            with patch("core.smart.v1.measurement.run_logged", side_effect=run), patch(
                "core.smart.v1.measurement.parse_vmaf_json",
                side_effect=[VmafWindowScore(80, 79, 78, 80), VmafWindowScore(96, 95, 94, 96),
                             VmafWindowScore(97, 96, 95, 97)],
            ) as parse:
                options = dict(cancel_check=None, process_callback=None, plan=session.exact_plan,
                               window_durations_sec=[5.0] * 3, measurement_cache=cache)
                rejected = score_candidate(session.ffmpeg_path, session.item, references, 900_000,
                                           root, root, io.StringIO(), min_vmaf_target=90,
                                           window_order=[2, 0, 1], **options)
                self.assertEqual(rejected.segment_vmaf, [80])
                completed = score_candidate(session.ffmpeg_path, session.item, references, 900_000,
                                            root, root, io.StringIO(), **options)
                self.assertEqual(completed.segment_vmaf, [96, 97, 80])
                self.assertEqual(parse.call_count, 3)
