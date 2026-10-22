from __future__ import annotations

import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.models import BackendChoice, EncodeOptions, EncodePlanItem
from core.smart.v1.measurement import SMART_ERROR_TAIL_CHARS, SmartCommandError
from core.smart.v1.measurement import run_logged as _run_logged
from core.smart.v1.measurement import score_candidate as _score_candidate
from core.smart.v1.runtime import (
    SOURCE_DECODE_SOFTWARE,
    SOURCE_DECODE_VIDEOTOOLBOX,
    AnalysisTier,
    build_analysis_execution_plan,
    software_source_plan,
)
from core.smart.v1.measurement import SampleWindow
from core.smart.v1.measurement import build_loopback_score_command as _build_loopback_score_command
from core.smart.v1.measurement import build_reference as _build_reference
from core.smart.v1.vmaf import VMAF_STANDARD_MODEL, VmafEncodeMetadata

from _smart_quality_fixtures import _item
from _analysis_runtime_fixtures import _capabilities, _encoder


class SmartCommandAndMeasurementTestCase(unittest.TestCase):
    def test_smart_process_uses_devnull_stdin_and_preserves_process_kwargs(self) -> None:
        class SuccessfulProcess:
            stdout = iter(["ok\n"])

            def wait(self, *_args, **_kwargs) -> int:
                return 0

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "smart.log"
            with (
                patch(
                    "core.smart.v1.measurement.hidden_popen_kwargs",
                    return_value={"creationflags": 0x08000000},
                ),
                patch("core.smart.v1.measurement.subprocess.Popen", return_value=SuccessfulProcess()) as popen,
                log_path.open("w", encoding="utf-8") as log_file,
            ):
                _run_logged(
                    ["ffmpeg", "-i", "input.mp4"],
                    log_file,
                    cancel_check=None,
                    process_callback=None,
                    cwd=root,
                    phase="reference extraction",
                )

        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["cwd"], root)
        self.assertEqual(kwargs["creationflags"], 0x08000000)

    def test_smart_process_start_oserror_is_flushed_to_log_with_phase(self) -> None:
        class FlushTrackingLog(io.StringIO):
            flush_count = 0

            def flush(self) -> None:
                self.flush_count += 1
                super().flush()

        os_error = "cannot launch ffmpeg: " + ("x" * (SMART_ERROR_TAIL_CHARS + 100))
        log_file = FlushTrackingLog()
        with (
            patch("core.smart.v1.measurement.subprocess.Popen", side_effect=OSError(os_error)),
            self.assertRaises(RuntimeError) as raised,
        ):
            _run_logged(
                ["ffmpeg", "-i", "input.mp4"],
                log_file,
                cancel_check=None,
                process_callback=None,
                phase="reference extraction",
            )

        contents = log_file.getvalue()
        self.assertIn("ffmpeg -i input.mp4", contents)
        self.assertIn("reference extraction", contents)
        self.assertIn("cannot launch ffmpeg", contents)
        self.assertIn("[smart process start failed]", contents)
        self.assertLessEqual(contents.count("x"), SMART_ERROR_TAIL_CHARS)
        self.assertGreaterEqual(log_file.flush_count, 2)
        self.assertIn("reference extraction", str(raised.exception))

    def test_smart_command_failure_identifies_phase_command_and_bounded_tail(self) -> None:
        class FailedProcess:
            stdout = iter(["initial output\n", "x" * (SMART_ERROR_TAIL_CHARS + 100) + "\n"])

            def wait(self, *_args, **_kwargs) -> int:
                return 17

            def terminate(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "smart.log"
            with (
                patch("core.smart.v1.measurement.subprocess.Popen", return_value=FailedProcess()),
                log_path.open("w", encoding="utf-8") as log_file,
                self.assertRaises(SmartCommandError) as raised,
            ):
                _run_logged(
                    ["ffmpeg", "-i", "input.mp4"],
                    log_file,
                    cancel_check=None,
                    process_callback=None,
                    phase="VMAF scoring",
                )
        self.assertIn("VMAF scoring", str(raised.exception))
        self.assertIn("exit code 17", str(raised.exception))
        self.assertIn("ffmpeg -i input.mp4", str(raised.exception))
        self.assertLessEqual(len(raised.exception.output_tail), SMART_ERROR_TAIL_CHARS)

    def test_vmaf_scoring_uses_relative_log_path_and_measured_sample_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 100_000)
            item = _item(source, root / "output.mp4", EncodeOptions())
            reference = root / "reference.mkv"
            reference.write_bytes(b"reference")
            log_path = root / "smart.log"
            log_path.write_text("", encoding="utf-8")
            score_calls: list[dict[str, object]] = []

            def fake_run(command: list[str], _log_file: object, **kwargs: object) -> None:
                phase = kwargs.get("phase")
                if phase == "candidate encode":
                    Path(command[-1]).write_bytes(b"c" * 1_000)
                elif phase == "VMAF scoring":
                    score_calls.append({"command": command, **kwargs})
                    filter_graph = command[command.index("-filter_complex") + 1]
                    match = re.search(r"log_path='([^']+)'", filter_graph)
                    self.assertIsNotNone(match)
                    json_name = match.group(1) if match else ""
                    self.assertEqual(Path(json_name).name, json_name)
                    self.assertNotIn(str(root), filter_graph)
                    cwd = kwargs["cwd"]
                    assert isinstance(cwd, Path)
                    (cwd / json_name).write_text(
                        json.dumps(
                            {
                                "pooled_metrics": {"vmaf": {"mean": 96.0}},
                                "frames": [
                                    {"frameNum": index, "metrics": {"vmaf": 96.0}}
                                    for index in range(300)
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )

            with (
                patch("core.smart.v1.measurement.run_logged", side_effect=fake_run),
                log_path.open("a", encoding="utf-8") as smart_log,
            ):
                result = _score_candidate(
                    Path("ffmpeg"),
                    item,
                    [reference],
                    1_000_000,
                    root,
                    root,
                    smart_log,
                    window_durations_sec=[10.0],
                    audio_bitrate_bps=128_000,
                    source_bytes=source.stat().st_size,
                    cancel_check=None,
                    process_callback=None,
                )

            self.assertEqual(len(score_calls), 1)
            score_command = score_calls[0]["command"]
            self.assertIsInstance(score_command, list)
            assert isinstance(score_command, list)
            self.assertEqual(score_command[score_command.index("-loglevel") + 1], "error")
            filter_graph = str(score_command[score_command.index("-filter_complex") + 1])
            self.assertIn("settb=AVTB,setpts=PTS-STARTPTS", filter_graph)
            self.assertIn("n_threads=", filter_graph)
            self.assertIn("n_subsample=1", filter_graph)
            self.assertEqual(result.encoded_bytes, [1_000])
            self.assertEqual(result.encoded_durations_sec, [10.0])
            self.assertEqual(result.segment_vmaf, [96.0])
            self.assertEqual(result.segment_mean_vmaf, [96.0])
            self.assertEqual(result.segment_p10_vmaf, [96.0])
            self.assertEqual(result.segment_worst_1s_vmaf, [96.0])
            self.assertEqual(result.segment_quality_scores, [96.0])
            self.assertEqual(result.observed_video_bitrate_bps, 800)
            self.assertIsNotNone(result.predicted_output_bytes)
            self.assertIsNotNone(result.predicted_output_ratio)


class SmartReferenceAndScoringTestCase(unittest.TestCase):
    def test_ffv1_reference_never_gets_videotoolbox_by_default_helper_can_add_it(self) -> None:
        item = EncodePlanItem(
            source_path=Path("movie.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            options=EncodeOptions(),
        )
        software = _build_reference(Path("ffmpeg"), item, SampleWindow(1.0, 5.0), Path("ref.mkv"))
        hardware = _build_reference(
            Path("ffmpeg"),
            item,
            SampleWindow(1.0, 5.0),
            Path("ref.mkv"),
            decode_acceleration=SOURCE_DECODE_VIDEOTOOLBOX,
        )
        self.assertNotIn("-hwaccel", software)
        self.assertEqual(hardware[hardware.index("-hwaccel") + 1], "videotoolbox")
        self.assertLess(hardware.index("-hwaccel"), hardware.index("-i"))
        self.assertIn("ffv1", software)
        self.assertIn("settb=AVTB,setpts=PTS-STARTPTS", software)

    def test_loopback_command_uses_dec_and_keeps_legacy_builder(self) -> None:
        encoder = _encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX)
        item = EncodePlanItem(
            source_path=Path("movie.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=encoder,
            options=EncodeOptions(),
            target_video_bitrate_bps=1_000_000,
        )
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=encoder,
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(loopback_decoder=True),
            enable_loopback=True,
        )
        command = _build_loopback_score_command(
            Path("ffmpeg"),
            item,
            SampleWindow(0.0, 5.0),
            Path("cand.mkv"),
            plan,
            model_spec=VMAF_STANDARD_MODEL,
            encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
            log_name="vmaf.json",
        )
        self.assertEqual(command[command.index("-loglevel") + 1], "error")
        self.assertIn("-dec:v", command)
        self.assertIn("hevc", command)
        self.assertIn("[dec:v]", command[command.index("-filter_complex") + 1])

    def test_videotoolbox_reference_failure_retries_software(self) -> None:
        item = EncodePlanItem(
            source_path=Path("movie.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            options=EncodeOptions(),
        )
        from core.smart.v1.measurement import SampleWindow, SmartCommandError

        command = _build_reference(
            Path("ffmpeg"),
            item,
            SampleWindow(0.0, 5.0),
            Path("ref.mkv"),
            decode_acceleration=SOURCE_DECODE_VIDEOTOOLBOX,
        )
        self.assertEqual(command[command.index("-hwaccel") + 1], "videotoolbox")
        assert item.encoder_info is not None
        fallback = software_source_plan(
            build_analysis_execution_plan(
                tier=AnalysisTier.EXACT,
                encoder_info=item.encoder_info,
                production_preset=None,
                production_two_pass=False,
                capabilities=_capabilities(),
            ),
            reason="VideoToolbox operation failed",
        )
        self.assertEqual(fallback.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
        self.assertIsInstance(SmartCommandError(1, ["ffmpeg"], "reference extraction", "vt fail"), SmartCommandError)

    def test_score_candidate_early_rejects_remaining_windows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.mov"
            source.write_bytes(b"s" * 100_000)
            item = EncodePlanItem(
                source_path=source,
                output_path=root / "out.mp4",
                media_info=None,
                encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
                options=EncodeOptions(encoder_preset="slow", min_vmaf=95.0),
            )
            from core.models import MediaInfo

            item.media_info = MediaInfo(
                path=source,
                duration=60.0,
                format_bitrate_bps=4_000_000,
                video_bitrate_bps=3_000_000,
                audio_bitrate_bps=128_000,
                width=1920,
                height=1080,
                fps=30.0,
                video_codec="h264",
                audio_codec="aac",
                audio_stream_count=1,
            )
            references = [root / "r0.mkv", root / "r1.mkv", root / "r2.mkv"]
            for path in references:
                path.write_bytes(b"ref")
            scored: list[int] = []

            def fake_run(command, _log, **kwargs):
                phase = kwargs.get("phase")
                if phase == "candidate encode":
                    Path(command[-1]).write_bytes(b"c" * 1_000)
                elif phase == "VMAF scoring":
                    scored.append(1)
                    graph = command[command.index("-filter_complex") + 1]
                    name = graph.split("log_path='")[1].split("'")[0]
                    (kwargs["cwd"] / name).write_text(
                        json.dumps(
                            {
                                "pooled_metrics": {"vmaf": {"mean": 90.0}},
                                "frames": [
                                    {"frameNum": index, "metrics": {"vmaf": 90.0}}
                                    for index in range(150)
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )

            log_path = root / "smart.log"
            with log_path.open("w", encoding="utf-8") as log_file, patch(
                "core.smart.v1.measurement.run_logged", side_effect=fake_run
            ):
                result = _score_candidate(
                    Path("ffmpeg"),
                    item,
                    references,
                    900_000,
                    root,
                    root,
                    log_file,
                    cancel_check=None,
                    process_callback=None,
                    window_durations_sec=[5.0, 5.0, 5.0],
                    min_vmaf_target=95.0,
                    window_order=[1, 0, 2],
                )
            self.assertEqual(len(scored), 1)
            self.assertEqual(result.segment_vmaf, [90.0])
            self.assertLess(result.min_vmaf, 95.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
