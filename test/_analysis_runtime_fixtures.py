from __future__ import annotations

import io

from core.smart.v1.bitrate import calculate_smart_bitrate_budget
from core.smart.v1.session import AnalysisSession
from core.smart.v1.runtime import AnalysisTier, build_analysis_execution_plan

from dataclasses import replace
from pathlib import Path

from core.smart.v1.runtime import AnalysisCapabilities
from core.models import (
    BackendChoice,
    CodecChoice,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
    MediaInfo,
)
from core.smart.v1.sampling.planner import PlannedWindow, SamplePlan
from core.smart.v1.sampling.scout import SamplingResult


def _capabilities(**overrides: object) -> AnalysisCapabilities:
    payload: dict[str, object] = dict(
        libvmaf=True,
        libvmaf_cuda=False,
        loopback_decoder=False,
        hwaccels=frozenset({"videotoolbox"}),
        filters=frozenset({"libvmaf", "scale_vt"}),
        encoders=frozenset({"libx265", "libsvtav1", "hevc_videotoolbox"}),
        scale_vt=True,
        scale_cuda=False,
        videotoolbox_hwaccel=True,
        cuda_hwaccel=False,
        videotoolbox_prio_speed=True,
    )
    payload.update(overrides)
    return AnalysisCapabilities(**payload)  # type: ignore[arg-type]


def _three_window_sampling() -> SamplingResult:
    windows = tuple(
        PlannedWindow(
            id=f"search:test-{index}",
            start_sec=float(index * 20),
            duration_sec=5.0,
            reasons=("test",),
        )
        for index in range(3)
    )
    return SamplingResult(SamplePlan((), windows, (), False), ())


def _sampling_with_holdout() -> SamplingResult:
    base = _three_window_sampling().plan
    holdouts = (
        PlannedWindow(
            id=f"holdout:test-{index}",
            start_sec=start,
            duration_sec=5.0,
            reasons=("test_holdout",),
            scout_id=f"scout-holdout-{index}",
        )
        for index, start in enumerate((50.0, 55.0), 1)
    )
    reserve = PlannedWindow(
        id="reserve:test",
        start_sec=45.0,
        duration_sec=5.0,
        reasons=("test_reserve",),
        scout_id="scout-reserve",
    )
    return SamplingResult(
        SamplePlan(
            base.scout_windows,
            base.search_windows,
            tuple(holdouts),
            False,
            reserve_windows=(reserve,),
        ),
        (),
    )


def _encoder(name: str, backend: BackendChoice, *, two_pass: bool = False, preset: str | None = None) -> EncoderInfo:
    return EncoderInfo(
        codec=CodecChoice.AV1 if "av1" in name or name == "libsvtav1" else CodecChoice.HEVC,
        backend=backend,
        encoder_name=name,
        supports_two_pass=two_pass,
        default_preset=preset,
    )


def _analysis_item(root: Path, *, max_refinement_rounds: int = 2) -> EncodePlanItem:
    source = root / "source.mov"
    with source.open("wb") as fh:
        fh.truncate(100_000_000)
    options = EncodeOptions(
        encoder_preset="slow",
        max_output_ratio=0.8,
        analysis_settings=replace(
            EncodeOptions().analysis_settings,
            preferred_vmaf_margin=0.0,
            max_refinement_rounds=max_refinement_rounds,
        ),
    )
    return EncodePlanItem(
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
            audio_stream_count=1,
            pix_fmt="yuv420p",
            color_transfer="bt709",
        ),
        encoder_info=_encoder("libx265", BackendChoice.CPU, two_pass=True, preset="slow"),
        options=options,
    )


def _session(root: Path) -> AnalysisSession:
    item = _analysis_item(root)
    assert item.encoder_info is not None
    plans = {
        tier: build_analysis_execution_plan(
            tier=tier,
            encoder_info=item.encoder_info,
            production_preset=item.options.encoder_preset,
            production_two_pass=item.options.two_pass,
            capabilities=_capabilities(),
        )
        for tier in AnalysisTier
    }
    return AnalysisSession(
        ffmpeg_path=root / "ffmpeg", item=item, workdir=root,
        temp_root=root, log_file=io.StringIO(),
        budget=calculate_smart_bitrate_budget(item),
        exact_plan=plans[AnalysisTier.EXACT], coarse_plan=plans[AnalysisTier.COARSE],
        planned_search=list(_three_window_sampling().plan.search_windows),
        scout_observations=[], initial_candidates=[],
    )
