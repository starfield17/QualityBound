from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.smart.v1.receipts import ANALYSIS_RECEIPT_SCHEMA_VERSION, load_analysis_receipt
from core.smart.v1.runtime import VmafBackend
from core.models import (
    AnalysisReceipt,
    BackendChoice,
    EncodeOptions,
    EncodePlanItem,
    MediaInfo,
    QualityCandidateResult,
    VmafViewingContext,
)
from core.smart.v1.cache import (
    SMART_ANALYSIS_ALGORITHM_VERSION,
    SMART_SAMPLE_SCHEME_VERSION,
    measurement_configuration_fingerprint,
)

from _analysis_runtime_fixtures import _encoder


class SmartReceiptIdentityTestCase(unittest.TestCase):
    def test_old_receipt_schema_is_ignored_and_backend_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = AnalysisReceipt(
                schema_version=1,
                measurement_fingerprint="a" * 64,
                source_identity={"path": "s"},
                ffmpeg_identity={"path": "f"},
                encoder_identity={"encoder": "libx265"},
                sample_scheme_version=1,
                sample_windows=[(0.0, 10.0)],
                candidates=[QualityCandidateResult(video_bitrate_bps=1_000_000, min_vmaf=96.0)],
            )
            path = root / "analysis" / "receipts" / f"{'a' * 64}.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "measurement_fingerprint": old.measurement_fingerprint,
                        "source_identity": old.source_identity,
                        "ffmpeg_identity": old.ffmpeg_identity,
                        "encoder_identity": old.encoder_identity,
                        "sample_scheme_version": 1,
                        "sample_windows": old.sample_windows,
                        "candidates": [],
                        "created_at": "",
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(load_analysis_receipt(root, "a" * 64))

            source = root / "source.mov"
            source.write_bytes(b"source")
            ffmpeg = root / "ffmpeg"
            ffmpeg.write_bytes(b"binary")
            item = EncodePlanItem(
                source_path=source,
                output_path=root / "out.mp4",
                media_info=MediaInfo(
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
                    pix_fmt="yuv420p",
                    bit_depth=8,
                ),
                encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
                options=EncodeOptions(encoder_preset="slow"),
            )
            cpu = measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU)
            cuda = measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CUDA)
            self.assertNotEqual(cpu, cuda)
            standard_display = replace(
                item,
                options=replace(
                    item.options,
                    viewing_context=VmafViewingContext.STANDARD_DISPLAY,
                ),
            )
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, standard_display),
                cpu,
            )
            self.assertEqual(SMART_SAMPLE_SCHEME_VERSION, 7)
            self.assertEqual(SMART_ANALYSIS_ALGORITHM_VERSION, 9)
            self.assertEqual(ANALYSIS_RECEIPT_SCHEMA_VERSION, 5)
            from core.smart.v1.cache import measurement_configuration_payload

            payload = measurement_configuration_payload(ffmpeg, item, vmaf_backend=VmafBackend.CPU)
            self.assertEqual(payload["sample_scheme_version"], 7)
            self.assertEqual(payload["vmaf_subsample"], 1)
            self.assertEqual(payload["vmaf_backend"], "cpu")
            self.assertEqual(payload["analysis_algorithm_version"], 9)
            self.assertEqual(payload["vmaf_resolution_mode"], "display_model_canvas")
            self.assertEqual(payload["vmaf_generation"], "v1")
            self.assertEqual(payload["vmaf_model"], "vmaf_v1.0.16_3d0h")
            self.assertEqual(payload["vmaf_measurement_pix_fmt"], "yuv420p10le")
            self.assertEqual(payload["vmaf_measurement_bit_depth"], 10)
            self.assertEqual(payload["vmaf_measurement_pipeline_version"], 3)
            self.assertEqual(payload["vmaf_scale_algorithm"], "bicubic")
            self.assertEqual(
                payload["vmaf_aspect_policy"], "square_pixels_fit_and_even_pad"
            )
            self.assertEqual(payload["candidate_encode_bit_depth"], 8)
            self.assertEqual(payload["vmaf_pooling"], "smart_v2_temporal_mean_worst_1s_v1")
            self.assertNotIn("n_threads", payload)
            self.assertNotIn("vmaf_threads", payload)
            assert item.media_info is not None
            item.media_info.fps = 60.0
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                cpu,
            )
            item.media_info.fps = 30.0
            item.options.pix_fmt = "p010le"
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                cpu,
            )
            item.options.pix_fmt = "yuv420p"
            item.media_info.bit_depth = 10
            self.assertNotEqual(
                measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                cpu,
            )
            item.media_info.bit_depth = 8
            with patch("core.smart.v1.cache.SMART_SAMPLE_SCHEME_VERSION", 99):
                self.assertNotEqual(
                    measurement_configuration_fingerprint(ffmpeg, item, vmaf_backend=VmafBackend.CPU),
                    cpu,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
