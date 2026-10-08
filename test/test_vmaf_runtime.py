from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.models import MediaInfo, VmafBackend, VmafViewingContext
from core.ffmpeg.probe import probe_media_info
from core.smart.v1.vmaf import (
    COARSE_VMAF_SUBSAMPLE,
    EXACT_VMAF_SUBSAMPLE,
    PTS_RESET_FILTER,
    VMAF_4K_HFR_MODEL,
    VMAF_4K_MODEL,
    VMAF_STANDARD_HFR_MODEL,
    VMAF_STANDARD_MODEL,
    VMAF_MEASUREMENT_PIPELINE_VERSION,
    InvalidVmafSubsample,
    VmafEncodeMetadata,
    _probe_vmaf_runtime_cached,
    build_cpu_vmaf_command,
    build_cuda_vmaf_command,
    build_libvmaf_option,
    build_vmaf_model_config,
    build_vmaf_probe_command,
    infer_bit_depth_from_pix_fmt,
    parse_vmaf_json,
    parse_vmaf_score,
    probe_vmaf_runtime,
    quote_libvmaf_model_config,
    select_vmaf_model,
    select_vmaf_runtime,
    validate_vmaf_subsample,
    vmaf_thread_budget,
)


def _media(*, width: int = 1920, height: int = 1080, fps: float | None = 30.0) -> MediaInfo:
    return MediaInfo(
        path=Path("source.mkv"),
        duration=10.0,
        format_bitrate_bps=2_000_000,
        video_bitrate_bps=1_800_000,
        audio_bitrate_bps=128_000,
        width=width,
        height=height,
        fps=fps,
        video_codec="hevc",
        audio_codec="aac",
    )


class VmafRuntimeTestCase(unittest.TestCase):
    def tearDown(self) -> None:
        _probe_vmaf_runtime_cached.cache_clear()

    def test_exact_and_coarse_subsamples_are_odd(self) -> None:
        self.assertEqual(validate_vmaf_subsample(EXACT_VMAF_SUBSAMPLE), 1)
        self.assertEqual(validate_vmaf_subsample(COARSE_VMAF_SUBSAMPLE), 3)
        with self.assertRaises(InvalidVmafSubsample):
            validate_vmaf_subsample(2)
        with self.assertRaises(InvalidVmafSubsample):
            validate_vmaf_subsample(0)

    def test_thread_budget_is_at_least_one_and_respects_active_jobs(self) -> None:
        with patch("core.smart.v1.vmaf.os.cpu_count", return_value=16):
            self.assertEqual(vmaf_thread_budget(1), 8)
            self.assertEqual(vmaf_thread_budget(2), 7)
        with patch("core.smart.v1.vmaf.os.cpu_count", return_value=4):
            self.assertEqual(vmaf_thread_budget(1), 3)
        with patch("core.smart.v1.vmaf.os.cpu_count", return_value=None):
            self.assertGreaterEqual(vmaf_thread_budget(1), 1)

    def test_model_selection_covers_resolution_hfr_boundary_and_unknown_fps(self) -> None:
        self.assertEqual(select_vmaf_model(_media(fps=24.0)), VMAF_STANDARD_MODEL)
        self.assertEqual(select_vmaf_model(_media(fps=49.999)), VMAF_STANDARD_MODEL)
        self.assertEqual(select_vmaf_model(_media(fps=50.0)), VMAF_STANDARD_HFR_MODEL)
        self.assertEqual(select_vmaf_model(_media(fps=59.94)), VMAF_STANDARD_HFR_MODEL)
        self.assertEqual(select_vmaf_model(_media(fps=60.0)), VMAF_STANDARD_HFR_MODEL)
        self.assertEqual(select_vmaf_model(_media(width=3840, height=2160, fps=30)), VMAF_4K_MODEL)
        self.assertEqual(select_vmaf_model(_media(width=3840, height=2160, fps=60)), VMAF_4K_HFR_MODEL)
        self.assertEqual(select_vmaf_model(_media(width=2560, height=1440)), VMAF_STANDARD_MODEL)
        self.assertEqual(select_vmaf_model(_media(width=1080, height=1920)), VMAF_STANDARD_MODEL)
        self.assertEqual(select_vmaf_model(_media(fps=None)), VMAF_STANDARD_MODEL)
        self.assertEqual(
            select_vmaf_model(
                _media(width=3840, height=2160, fps=30),
                VmafViewingContext.STANDARD_DISPLAY,
            ),
            VMAF_STANDARD_MODEL,
        )
        self.assertEqual(
            select_vmaf_model(
                _media(width=3840, height=2160, fps=60),
                VmafViewingContext.STANDARD_DISPLAY,
            ),
            VMAF_STANDARD_HFR_MODEL,
        )

    def test_bit_depth_inference_is_conservative(self) -> None:
        for pix_fmt in ("yuv420p", "nv12"):
            self.assertEqual(infer_bit_depth_from_pix_fmt(pix_fmt), 8)
        for pix_fmt in ("yuv420p10le", "p010le", "gbrp10le"):
            self.assertEqual(infer_bit_depth_from_pix_fmt(pix_fmt), 10)
        self.assertEqual(infer_bit_depth_from_pix_fmt("yuv420p9le"), 9)
        self.assertEqual(infer_bit_depth_from_pix_fmt("yuv444p12le"), 12)
        self.assertIsNone(infer_bit_depth_from_pix_fmt("unknown"))
        self.assertIsNone(infer_bit_depth_from_pix_fmt(None))

    def test_media_probe_prefers_raw_bit_depth_then_falls_back_to_pix_fmt(self) -> None:
        base = {
            "format": {"duration": "10", "bit_rate": "2000000"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 1920,
                    "height": 1080,
                    "avg_frame_rate": "30/1",
                    "pix_fmt": "yuv420p10le",
                    "bits_per_raw_sample": "12",
                    "bit_rate": "1800000",
                }
            ],
        }
        with patch("core.ffmpeg.probe.ffprobe_json", return_value=base):
            self.assertEqual(probe_media_info(Path("ffprobe"), Path("source.mkv")).bit_depth, 12)
        del base["streams"][0]["bits_per_raw_sample"]
        with patch("core.ffmpeg.probe.ffprobe_json", return_value=base):
            self.assertEqual(probe_media_info(Path("ffprobe"), Path("source.mkv")).bit_depth, 10)

    def test_cambi_model_config_has_one_precisely_escaped_nested_dictionary(self) -> None:
        metadata = VmafEncodeMetadata(1280, 720, 8)
        config = build_vmaf_model_config(VMAF_STANDARD_MODEL, metadata)
        self.assertEqual(
            config,
            "version=vmaf_v1.0.16_3d0h:cambi.enc_width=1280:"
            "cambi.enc_height=720:cambi.enc_bitdepth=8",
        )
        self.assertEqual(
            quote_libvmaf_model_config(config),
            "'version=vmaf_v1.0.16_3d0h\\:cambi.enc_width=1280\\:"
            "cambi.enc_height=720\\:cambi.enc_bitdepth=8'",
        )

    def test_cpu_command_normalizes_both_inputs_to_model_canvas_and_10_bit(self) -> None:
        command = build_cpu_vmaf_command(
            Path("ffmpeg"),
            distorted_path=Path("dist.mkv"),
            reference_path=Path("ref.mkv"),
            model_spec=VMAF_STANDARD_MODEL,
            encode_metadata=VmafEncodeMetadata(1280, 720, 8),
            log_name="vmaf.json",
            n_threads=8,
            n_subsample=3,
        )
        graph = command[command.index("-filter_complex") + 1]
        self.assertEqual(PTS_RESET_FILTER, "settb=AVTB,setpts=PTS-STARTPTS")
        self.assertEqual(VMAF_MEASUREMENT_PIPELINE_VERSION, 3)
        self.assertEqual(command[command.index("-loglevel") + 1], "error")
        self.assertEqual(graph.count("trunc(iw*sar/2)*2"), 2)
        self.assertEqual(graph.count("scale=1920:1080"), 2)
        self.assertEqual(graph.count("force_original_aspect_ratio=decrease"), 2)
        self.assertEqual(graph.count("force_divisible_by=2"), 2)
        self.assertEqual(graph.count("flags=bicubic"), 4)
        self.assertEqual(graph.count("pad=1920:1080"), 2)
        self.assertEqual(graph.count("trunc((ow-iw)/4)*2"), 2)
        self.assertEqual(graph.count("setsar=1"), 4)
        self.assertEqual(graph.count("format=yuv420p10le"), 2)
        self.assertIn("vmaf_v1.0.16_3d0h", graph)
        self.assertIn("cambi.enc_width=1280", graph)
        self.assertIn("cambi.enc_height=720", graph)
        self.assertIn("cambi.enc_bitdepth=8", graph)
        self.assertIn("n_threads=8", graph)
        self.assertIn("n_subsample=3", graph)
        self.assertIn("log_path='vmaf.json'", graph)
        self.assertIn("libvmaf=", graph)
        self.assertNotIn("libvmaf_cuda", graph)

    def test_probe_uses_model_frame_rate_and_multiple_frames(self) -> None:
        standard = build_vmaf_probe_command(
            Path("ffmpeg"), VMAF_STANDARD_MODEL, VmafBackend.CPU
        )
        hfr = build_vmaf_probe_command(
            Path("ffmpeg"), VMAF_STANDARD_HFR_MODEL, VmafBackend.CPU
        )
        self.assertEqual(standard.count("testsrc2=size=320x180:rate=30:duration=1"), 2)
        self.assertEqual(standard[standard.index("-frames:v") + 1], "6")
        self.assertEqual(hfr.count("testsrc2=size=320x180:rate=60:duration=1"), 2)
        self.assertEqual(hfr[hfr.index("-frames:v") + 1], "12")

    def test_score_parsers_reject_nonfinite_and_model_range_violations(self) -> None:
        self.assertEqual(parse_vmaf_score("VMAF score: 99.5", VMAF_STANDARD_MODEL), 99.5)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vmaf.json"
            for value in (float("nan"), float("inf"), -0.1, 100.1):
                path.write_text(
                    json.dumps(
                        {
                            "pooled_metrics": {"vmaf": {"mean": value}},
                            "frames": [{"frameNum": 0, "metrics": {"vmaf": value}}],
                        }
                    ),
                    encoding="utf-8",
                )
                with self.subTest(value=value), self.assertRaisesRegex(
                    RuntimeError, "invalid score"
                ):
                    parse_vmaf_json(path, VMAF_STANDARD_MODEL)

    def test_temporal_pooling_catches_one_second_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vmaf.json"
            frame_scores = [95.0] * 120 + [78.0] * 30
            mean = sum(frame_scores) / len(frame_scores)
            path.write_text(
                json.dumps(
                    {
                        "pooled_metrics": {"vmaf": {"mean": mean}},
                        "frames": [
                            {"frameNum": index, "metrics": {"vmaf": score}}
                            for index, score in enumerate(frame_scores)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = parse_vmaf_json(path, VMAF_STANDARD_MODEL, fps=30.0)

        self.assertGreater(result.mean, 90.0)
        self.assertEqual(result.p10, 78.0)
        self.assertEqual(result.worst_1s, 78.0)
        self.assertEqual(result.gate_score, 82.0)

    def test_temporal_pooling_does_not_use_single_frame_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "vmaf.json"
            frame_scores = [95.0] * 150
            frame_scores[75] = 78.0
            mean = sum(frame_scores) / len(frame_scores)
            path.write_text(
                json.dumps(
                    {
                        "pooled_metrics": {"vmaf": {"mean": mean}},
                        "frames": [
                            {"frameNum": index, "metrics": {"vmaf": score}}
                            for index, score in enumerate(frame_scores)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = parse_vmaf_json(path, VMAF_STANDARD_MODEL, fps=30.0)

        self.assertEqual(result.p10, 95.0)
        self.assertGreater(result.worst_1s, 94.0)
        self.assertGreater(result.gate_score, 94.0)
        self.assertNotEqual(result.gate_score, 78.0)

    def test_even_subsample_cannot_be_rendered(self) -> None:
        with self.assertRaises(InvalidVmafSubsample):
            build_libvmaf_option(
                model_spec=VMAF_STANDARD_MODEL,
                encode_metadata=VmafEncodeMetadata(None, None, None),
                log_path="vmaf.json",
                n_threads=1,
                n_subsample=2,
            )

    def test_v1_cuda_command_is_explicitly_unsupported(self) -> None:
        with self.assertRaisesRegex(ValueError, "VMAF v1 CUDA measurement is not implemented"):
            build_cuda_vmaf_command(
                Path("ffmpeg"),
                distorted_path=Path("distorted.mkv"),
                reference_path=Path("reference.mkv"),
                model_spec=VMAF_STANDARD_MODEL,
                encode_metadata=VmafEncodeMetadata(1920, 1080, 8),
                log_name="vmaf.json",
                n_threads=2,
                n_subsample=1,
            )

    def test_runtime_probe_is_cached_per_model_and_ffmpeg_identity(self) -> None:
        success = subprocess.CompletedProcess([], 0, "", "VMAF score: 100.0\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg = Path(temp_dir) / "ffmpeg"
            ffmpeg.write_bytes(b"one")
            with patch("core.smart.v1.vmaf._run_capture", return_value=success) as run:
                self.assertTrue(probe_vmaf_runtime(ffmpeg, VMAF_STANDARD_MODEL, VmafBackend.CPU).runnable)
                self.assertTrue(probe_vmaf_runtime(ffmpeg, VMAF_STANDARD_MODEL, VmafBackend.CPU).runnable)
                self.assertTrue(probe_vmaf_runtime(ffmpeg, VMAF_4K_MODEL, VmafBackend.CPU).runnable)
                self.assertEqual(run.call_count, 2)
                ffmpeg.write_bytes(b"changed-size")
                self.assertTrue(probe_vmaf_runtime(ffmpeg, VMAF_STANDARD_MODEL, VmafBackend.CPU).runnable)
                self.assertEqual(run.call_count, 3)

    def test_backend_policy_requires_probe_success_before_selection(self) -> None:
        success = subprocess.CompletedProcess([], 0, "", "VMAF score: 100.0\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg = Path(temp_dir) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            with patch("core.smart.v1.vmaf._run_capture", return_value=success):
                support = select_vmaf_runtime(
                    ffmpeg,
                    VMAF_STANDARD_MODEL,
                    backend_policy=(VmafBackend.CUDA, VmafBackend.CPU),
                )
        self.assertTrue(support.runnable)
        self.assertEqual(support.backend, VmafBackend.CPU)

    def test_runtime_probe_accepts_score_written_to_stdout(self) -> None:
        success = subprocess.CompletedProcess([], 0, "VMAF score: 100.0\n", "")
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg = Path(temp_dir) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            with patch("core.smart.v1.vmaf._run_capture", return_value=success):
                support = probe_vmaf_runtime(ffmpeg, VMAF_STANDARD_MODEL, VmafBackend.CPU)
        self.assertTrue(support.runnable)

    def test_runtime_probe_rejects_nonfinite_score(self) -> None:
        result = subprocess.CompletedProcess([], 0, "", "VMAF score: nan\n")
        with tempfile.TemporaryDirectory() as temp_dir:
            ffmpeg = Path(temp_dir) / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            with patch("core.smart.v1.vmaf._run_capture", return_value=result):
                support = probe_vmaf_runtime(ffmpeg, VMAF_STANDARD_MODEL, VmafBackend.CPU)
        self.assertFalse(support.runnable)
        self.assertIn("invalid score", support.error_message or "")


if __name__ == "__main__":
    unittest.main()
