from __future__ import annotations

from core.models import CodecChoice


# Target-bitrate multipliers: how much of the source bitrate each codec
# should retain at default quality. HEVC is ~24% more efficient than source;
# AV1 is ~36% more efficient.
DEFAULT_RATIO = {
    CodecChoice.HEVC: 0.76,
    CodecChoice.AV1: 0.64,
}


def choose_ratio(codec: CodecChoice, ratio: float | None) -> float:
    if ratio is not None:
        if ratio <= 0:
            raise ValueError("ratio must be greater than 0")
        return ratio
    return DEFAULT_RATIO[codec]


def kbps_to_bps(kbps: float) -> int:
    return int(round(kbps * 1000))


def human_kbps(bps: int) -> str:
    return f"{bps / 1000:.0f} kbps"


def compute_target_video_bitrate(
    source_video_bps: int,
    ratio: float,
    min_video_kbps: int,
    max_video_kbps: int,
) -> int:
    target = int(round(source_video_bps * ratio))
    target = max(target, kbps_to_bps(min_video_kbps))
    if max_video_kbps > 0:
        target = min(target, kbps_to_bps(max_video_kbps))
    # Hard floor to prevent an unreasonably low bitrate slipping through,
    # even when both the ratio and user-supplied minimum are very small.
    return max(target, 50_000)
