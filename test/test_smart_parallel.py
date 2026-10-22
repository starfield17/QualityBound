from __future__ import annotations

import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.encoding import execute_plan_concurrent, execute_plan_item
from core.models import (
    BackendChoice,
    CodecChoice,
    EncodeOptions,
    EncodePlan,
    EncodePlanItem,
    EncoderInfo,
    OperationCancelledError,
    QualitySearchResult,
    QualitySearchStatus,
    VmafBackend,
    VmafRuntimeSupport,
)
from core.smart.v1.workflow import analyze_quality
from core.smart.v1.cache import (
    measurement_configuration_fingerprint,
    quality_configuration_fingerprint,
)

from _smart_quality_fixtures import _item


class SmartParallelExecutionTestCase(unittest.TestCase):
    def _quality_result(self, max_bytes: int = 700) -> QualitySearchResult:
        return QualitySearchResult(
            status=QualitySearchStatus.FOUND,
            encoder_name="libx265",
            backend=BackendChoice.CPU,
            selected_video_bitrate_bps=500_000,
            min_vmaf=96.0,
            predicted_output_bytes=600,
            predicted_output_ratio=0.60,
            max_output_bytes=max_bytes,
            fingerprint="fingerprint",
        )

    def test_concurrent_workers_preserve_bindings_during_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            items: list[EncodePlanItem] = []
            for index in range(2):
                source = root / f"source-{index}.mov"
                source.write_bytes(b"s" * 1_000)
                item = _item(source, root / f"output-{index}.mp4", EncodeOptions(overwrite=True))
                if index == 1:
                    item.encoder_info = EncoderInfo(
                        codec=CodecChoice.HEVC,
                        backend=BackendChoice.NVENC,
                        encoder_name="hevc_nvenc",
                        supports_two_pass=False,
                        default_preset=None,
                    )
                    item.options = replace(item.options, backend=BackendChoice.NVENC)
                items.append(item)
            plan = EncodePlan(
                items=items,
                ffmpeg_path=Path("ffmpeg"),
                ffprobe_path=Path("ffprobe"),
                input_root=root,
                output_root=root,
            )
            active = 0
            max_active = 0
            observed_backends: set[BackendChoice] = set()
            counter_lock = threading.Lock()

            def fake_analysis(_ffmpeg, item, _workdir, _log_path, **_kwargs):
                nonlocal active, max_active
                self.assertIsNotNone(item.encoder_info)
                observed_backends.add(item.encoder_info.backend)
                with counter_lock:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.03)
                with counter_lock:
                    active -= 1
                return QualitySearchResult(
                    status=QualitySearchStatus.FOUND,
                    encoder_name=item.encoder_info.encoder_name,
                    backend=item.encoder_info.backend,
                    selected_video_bitrate_bps=500_000,
                    min_vmaf=96.0,
                    predicted_output_ratio=0.60,
                    max_output_bytes=700,
                    fingerprint=item.encoder_info.backend.value,
                )

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"x" * 600)

            with (
                patch("core.encoding.analysis.analyze_quality", side_effect=fake_analysis),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                results = execute_plan_concurrent(plan, root, max_workers=2)

            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.success for result in results))
            self.assertEqual(observed_backends, {BackendChoice.CPU, BackendChoice.NVENC})
            self.assertGreaterEqual(max_active, 1)
            self.assertLessEqual(max_active, 2)

    def test_queue_analyzes_every_item_before_any_encode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            items: list[EncodePlanItem] = []
            for index in range(3):
                source = root / f"source-{index}.mov"
                source.write_bytes(b"s" * 1_000)
                items.append(_item(source, root / f"output-{index}.mp4", EncodeOptions(overwrite=True)))
            plan = EncodePlan(
                items=items,
                ffmpeg_path=Path("ffmpeg"),
                ffprobe_path=Path("ffprobe"),
                input_root=root,
                output_root=root,
            )
            events: list[str] = []

            def fake_analysis(_ffmpeg, item, _workdir, _log_path, **_kwargs):
                events.append(f"analyze:{item.source_path.name}")
                time.sleep(0.01)
                return self._quality_result()

            def fake_run(cmd, *_args, **_kwargs) -> None:
                events.append(f"encode:{Path(cmd[-1]).name}")
                Path(cmd[-1]).write_bytes(b"x" * 600)

            with (
                patch("core.encoding.analysis.analyze_quality", side_effect=fake_analysis),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                results = execute_plan_concurrent(plan, root, max_workers=2)

            self.assertEqual(len(results), 3)
            self.assertTrue(all(result.success for result in results))
            analyze_events = [event for event in events if event.startswith("analyze:")]
            encode_events = [event for event in events if event.startswith("encode:")]
            self.assertEqual(len(analyze_events), 3)
            self.assertTrue(encode_events)
            last_analyze = max(index for index, event in enumerate(events) if event.startswith("analyze:"))
            first_encode = min(index for index, event in enumerate(events) if event.startswith("encode:"))
            self.assertLess(last_analyze, first_encode)

    def test_successful_output_is_published_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            output = root / "output.mp4"
            options = EncodeOptions(max_output_ratio=0.70, overwrite=True)
            item = _item(source, output, options)

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"x" * 600)

            with (
                patch("core.encoding.analysis.analyze_quality", return_value=self._quality_result()),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
            ):
                result = execute_plan_item(Path("ffmpeg"), item, root)

            self.assertTrue(result.success)
            self.assertEqual(output.stat().st_size, 600)

    def test_fingerprint_changes_when_backend_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "output.mp4", EncodeOptions())
            first = quality_configuration_fingerprint(ffmpeg, item)
            item.encoder_info = EncoderInfo(
                codec=CodecChoice.HEVC,
                backend=BackendChoice.NVENC,
                encoder_name="hevc_nvenc",
                supports_two_pass=False,
                default_preset="p6",
            )
            second = quality_configuration_fingerprint(ffmpeg, item)
            self.assertNotEqual(first, second)

    def test_search_controls_change_decision_but_not_measurement_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "output.mp4", EncodeOptions())
            measurement = measurement_configuration_fingerprint(ffmpeg, item)
            decision = quality_configuration_fingerprint(ffmpeg, item)

            item.options = replace(
                item.options,
                analysis_settings=replace(
                    item.options.analysis_settings,
                    exact_max_candidates=item.options.analysis_settings.exact_max_candidates + 1,
                    min_search_tolerance_bps=25_000,
                ),
            )

            self.assertEqual(measurement_configuration_fingerprint(ffmpeg, item), measurement)
            self.assertNotEqual(quality_configuration_fingerprint(ffmpeg, item), decision)

    def test_matching_analysis_cache_is_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"source")
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = _item(source, root / "output.mp4", EncodeOptions())
            cached = self._quality_result()
            cached.fingerprint = quality_configuration_fingerprint(ffmpeg, item)
            item.quality_search_result = cached
            with patch(
                "core.smart.v1.workflow.select_vmaf_runtime",
                return_value=VmafRuntimeSupport(VmafBackend.CPU, "vmaf_v1.0.16_3d0h", True),
            ) as detect:
                result = analyze_quality(ffmpeg, item, root, root / "log.txt")
            self.assertIs(result, cached)
            detect.assert_called_once()

    def test_cancelled_output_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 1_000)
            item = _item(source, root / "output.mp4", EncodeOptions(overwrite=True))

            def fake_run(cmd, *_args, **_kwargs) -> None:
                Path(cmd[-1]).write_bytes(b"partial")
                raise OperationCancelledError("cancel")

            with (
                patch("core.encoding.analysis.analyze_quality", return_value=self._quality_result()),
                patch("core.encoding.executor._run_logged_command", side_effect=fake_run),
                self.assertRaises(OperationCancelledError),
            ):
                execute_plan_item(Path("ffmpeg"), item, root)
            self.assertFalse(list(root.glob(".*.partial-*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
