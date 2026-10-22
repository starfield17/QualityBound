from __future__ import annotations

from pathlib import Path

from core.models import BackendChoice, EncodeOptions, EncodePlanItem, EncoderInfo, MediaInfo


def _media(path: Path, *, duration: float = 60.0, audio_streams: int = 1) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=duration,
        format_bitrate_bps=4_000_000,
        video_bitrate_bps=3_000_000,
        audio_bitrate_bps=128_000 * audio_streams,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        audio_stream_count=audio_streams,
        pix_fmt="yuv420p",
        color_transfer="bt709",
    )


def _item(source: Path, output: Path, options: EncodeOptions) -> EncodePlanItem:
    return EncodePlanItem(
        source_path=source,
        output_path=output,
        media_info=_media(source),
        encoder_info=EncoderInfo(
            codec=options.codec,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="slow",
        ),
        options=options,
    )
