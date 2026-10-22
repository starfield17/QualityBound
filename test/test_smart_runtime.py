from __future__ import annotations

import unittest
from pathlib import Path

from core.smart.v1.runtime import (
    SOURCE_DECODE_SOFTWARE,
    SOURCE_DECODE_VIDEOTOOLBOX,
    AnalysisDecodePolicy,
    AnalysisTier,
    VmafBackend,
    build_analysis_execution_plan,
    coarse_encoder_preset,
    cpu_vmaf_plan,
    format_analysis_capability_report,
    help_exposes_loopback_decoder,
    legacy_loopback_plan,
    parse_decoder_names,
    parse_filter_names,
    software_source_plan,
    source_decode_args,
)
from core.ffmpeg.commands import build_video_args
from core.models import BackendChoice, CodecChoice, EncodeOptions, EncodePlanItem
from core.smart.v1.concurrency import SMART_ANALYSIS_SEMAPHORE
from core.smart.v1.vmaf import COARSE_VMAF_SUBSAMPLE, EXACT_VMAF_SUBSAMPLE

from _analysis_runtime_fixtures import _capabilities, _encoder


class AnalysisCapabilityParsingTestCase(unittest.TestCase):
    def test_filter_and_decoder_parsers(self) -> None:
        filters = parse_filter_names(
            "Filters:\n"
            " .. libvmaf           VV->V      Calculate the VMAF\n"
            " TSC scale_vt         V->V       Scale Videotoolbox frames\n"
            " .. libvmaf_cuda      VV->V      CUDA VMAF\n"
        )
        self.assertEqual(filters, frozenset({"libvmaf", "scale_vt", "libvmaf_cuda"}))
        decoders = parse_decoder_names(" V..... loopback            Loopback decoder\n V....D hevc                HEVC")
        self.assertIn("loopback", decoders)

    def test_loopback_help_detection_and_legacy_fallback(self) -> None:
        self.assertTrue(help_exposes_loopback_decoder("  -dec[:stream] decoder   loopback decoder"))
        self.assertFalse(help_exposes_loopback_decoder("  -decode                  unused"))
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
            production_preset="slow",
            production_two_pass=True,
            capabilities=_capabilities(loopback_decoder=True),
            enable_loopback=True,
        )
        self.assertTrue(plan.use_loopback)
        fallback = legacy_loopback_plan(plan, reason="loopback failed")
        self.assertFalse(fallback.use_loopback)
        self.assertEqual(fallback.fallback_reason, "loopback failed")

    def test_capability_report_does_not_require_optional_features(self) -> None:
        report = format_analysis_capability_report(_capabilities(libvmaf_cuda=False, loopback_decoder=False))
        self.assertIn("CPU VMAF", report)
        self.assertIn("CUDA VMAF filter (not enabled for v1)", report)
        self.assertIn("Loopback decoder", report)
        self.assertIn("VideoToolbox", report)


class AnalysisExecutionPlanTestCase(unittest.TestCase):
    def test_coarse_uses_faster_preset_and_one_pass(self) -> None:
        coarse = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
            production_preset="slow",
            production_two_pass=True,
            capabilities=_capabilities(),
        )
        exact = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
            production_preset="slow",
            production_two_pass=True,
            capabilities=_capabilities(),
        )
        self.assertEqual(coarse.encoder_name, "libx265")
        self.assertEqual(coarse.encoder_preset, "fast")
        self.assertFalse(coarse.two_pass)
        self.assertEqual(coarse.vmaf_subsample, COARSE_VMAF_SUBSAMPLE)
        self.assertEqual(exact.encoder_preset, "slow")
        self.assertTrue(exact.two_pass)
        self.assertEqual(exact.vmaf_subsample, EXACT_VMAF_SUBSAMPLE)
        self.assertGreaterEqual(exact.vmaf_threads, 1)

    def test_svt_and_nvenc_coarse_presets_stay_on_the_same_encoder(self) -> None:
        self.assertEqual(coarse_encoder_preset("libsvtav1", "5"), "8")
        self.assertEqual(coarse_encoder_preset("hevc_nvenc", "p6"), "p4")
        svt = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=_encoder("libsvtav1", BackendChoice.CPU, preset="5"),
            production_preset="5",
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        self.assertEqual(svt.encoder_name, "libsvtav1")
        self.assertEqual(svt.encoder_preset, "8")

    def test_videotoolbox_coarse_adds_prio_speed_exact_does_not(self) -> None:
        encoder = _encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX)
        coarse = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=encoder,
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        exact = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=encoder,
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        self.assertEqual(coarse.encoder_extra_args, ("-prio_speed", "1"))
        self.assertEqual(exact.encoder_extra_args, ())
        self.assertEqual(coarse.source_decode_acceleration, SOURCE_DECODE_VIDEOTOOLBOX)
        item = EncodePlanItem(
            source_path=Path("in.mp4"),
            output_path=Path("out.mp4"),
            media_info=None,
            encoder_info=encoder,
            options=EncodeOptions(codec=CodecChoice.HEVC, backend=BackendChoice.VIDEOTOOLBOX),
            target_video_bitrate_bps=1_000_000,
        )
        exact_args = build_video_args(item, extra_args=exact.encoder_extra_args)
        coarse_args = build_video_args(item, extra_args=coarse.encoder_extra_args)
        self.assertEqual(exact_args[exact_args.index("-allow_sw") + 1], "0")
        self.assertNotIn("-prio_speed", exact_args)
        self.assertEqual(coarse_args[coarse_args.index("-allow_sw") + 1], "0")
        self.assertEqual(coarse_args[coarse_args.index("-prio_speed") + 1], "1")

    def test_software_policy_never_selects_videotoolbox_decode(self) -> None:
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
            decode_policy=AnalysisDecodePolicy.SOFTWARE,
        )
        self.assertEqual(plan.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
        self.assertEqual(source_decode_args(SOURCE_DECODE_SOFTWARE), [])
        self.assertEqual(source_decode_args(SOURCE_DECODE_VIDEOTOOLBOX), ["-hwaccel", "videotoolbox"])

    def test_av1_never_binds_videotoolbox_encoder_but_may_decode_with_vt(self) -> None:
        with self.assertRaises(ValueError):
            build_analysis_execution_plan(
                tier=AnalysisTier.EXACT,
                encoder_info=_encoder("av1_videotoolbox", BackendChoice.VIDEOTOOLBOX),
                production_preset="5",
                production_two_pass=False,
                capabilities=_capabilities(),
            )
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.COARSE,
            encoder_info=_encoder("libsvtav1", BackendChoice.CPU, preset="5"),
            production_preset="5",
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        self.assertEqual(plan.encoder_name, "libsvtav1")
        self.assertEqual(plan.source_decode_acceleration, SOURCE_DECODE_VIDEOTOOLBOX)

    def test_static_cuda_presence_does_not_enable_v1_cuda(self) -> None:
        cuda = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("hevc_nvenc", BackendChoice.NVENC, preset="p6"),
            production_preset="p6",
            production_two_pass=False,
            capabilities=_capabilities(
                libvmaf_cuda=True,
                scale_cuda=True,
                cuda_hwaccel=True,
                hwaccels=frozenset({"cuda"}),
                videotoolbox_hwaccel=False,
            ),
        )
        self.assertEqual(cuda.vmaf_backend, VmafBackend.CPU)
        fallback = cpu_vmaf_plan(cuda, reason="cuda vmaf failed")
        self.assertEqual(fallback.vmaf_backend, VmafBackend.CPU)
        self.assertFalse(fallback.use_loopback)

    def test_source_decode_fallback_drops_hardware(self) -> None:
        plan = build_analysis_execution_plan(
            tier=AnalysisTier.EXACT,
            encoder_info=_encoder("hevc_videotoolbox", BackendChoice.VIDEOTOOLBOX),
            production_preset=None,
            production_two_pass=False,
            capabilities=_capabilities(),
        )
        fallback = software_source_plan(plan, reason="vt failed")
        self.assertEqual(fallback.source_decode_acceleration, SOURCE_DECODE_SOFTWARE)
        self.assertEqual(fallback.encoder_name, "hevc_videotoolbox")


class SmartAnalysisConcurrencyTestCase(unittest.TestCase):
    def test_concurrency_limit_stays_resource_bounded(self) -> None:
        from core.smart.v1.concurrency import analysis_concurrency_limit

        self.assertEqual(analysis_concurrency_limit(cpu_count=4), 1)
        self.assertEqual(analysis_concurrency_limit(cpu_count=8), 2)
        self.assertLessEqual(SMART_ANALYSIS_SEMAPHORE._value, 2)
        self.assertGreaterEqual(SMART_ANALYSIS_SEMAPHORE._value, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
