from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from core.models import AudioMode, ContainerChoice, DecodeAcceleration, EncodePlanItem
from core.media.paths import passlog_prefix


def _null_sink() -> str:
    return "NUL" if os.name == "nt" else "/dev/null"


def build_input_acceleration_args(plan_item: EncodePlanItem) -> list[str]:
    acceleration = plan_item.options.decode_acceleration

    if acceleration == DecodeAcceleration.SOFTWARE:
        return []

    if acceleration == DecodeAcceleration.VIDEOTOOLBOX:
        return ["-hwaccel", "videotoolbox"]

    raise ValueError(f"Unsupported decode acceleration: {acceleration}")


def build_video_args(
    plan_item: EncodePlanItem,
    extra_args: Sequence[str] = (),
    *,
    muxing_to_null: bool = False,
) -> list[str]:
    if plan_item.encoder_info is None:
        raise ValueError("Encoding requires a bound encoder.")
    encoder = plan_item.encoder_info.encoder_name
    options = plan_item.options
    target_video_bps = plan_item.target_video_bitrate_bps
    args = [
        "-c:v",
        encoder,
        "-b:v",
        str(target_video_bps),
        "-pix_fmt",
        options.pix_fmt,
    ]

    # SVT-AV1 in current ffmpeg builds rejects VBV maxrate/bufsize in bitrate mode.
    if encoder != "libsvtav1":
        args += [
            "-maxrate",
            str(int(round(target_video_bps * options.maxrate_factor))),
            "-bufsize",
            str(int(round(target_video_bps * options.bufsize_factor))),
        ]

    if plan_item.options.encoder_preset:
        args += ["-preset", str(plan_item.options.encoder_preset)]

    if encoder in {"libx265", "hevc_videotoolbox"} and not muxing_to_null:
        # hvc1 tag is required for Apple QuickTime / macOS Finder playback, and
        # is only meaningful on the muxed output (not the null-muxer pass 1).
        args += ["-tag:v", "hvc1"]

    if encoder == "libx265":
        args += ["-x265-params", "log-level=error"]

    if encoder == "hevc_videotoolbox":
        args += ["-allow_sw", "0"]

    if extra_args:
        args += list(extra_args)

    return args


def build_audio_args(plan_item: EncodePlanItem) -> list[str]:
    options = plan_item.options
    if options.audio_mode == AudioMode.COPY:
        return ["-map", "0:a?", "-c:a", "copy"]
    return ["-map", "0:a?", "-c:a", "aac", "-b:a", options.audio_bitrate]


def build_subtitle_args(plan_item: EncodePlanItem) -> list[str]:
    options = plan_item.options
    if not options.copy_subtitles:
        return []
    if options.container == ContainerChoice.MKV:
        return ["-map", "0:s?", "-c:s", "copy"]
    if options.container == ContainerChoice.MP4:
        return ["-map", "0:s?", "-c:s", "mov_text"]
    return []


def build_common_output_args(plan_item: EncodePlanItem) -> list[str]:
    args = ["-map_metadata", "0", "-map_chapters", "0"]
    if plan_item.options.container == ContainerChoice.MP4:
        # Move the moov atom to the front so the file starts playing before
        # it is fully downloaded (progressive-download / streaming-friendly).
        args += ["-movflags", "+faststart"]
    return args


def build_encode_commands(
    ffmpeg_path: Path,
    plan_item: EncodePlanItem,
    workdir: Path,
    *,
    input_path: Path | None = None,
    output_path: Path | None = None,
    stage: str = "encode",
    extra_video_args: Sequence[str] = (),
) -> tuple[list[list[str]], Path | None]:
    if not plan_item.encoder_info:
        raise ValueError("Plan item is missing encoder information.")

    source_path = input_path or plan_item.source_path
    final_output = output_path or plan_item.output_path
    overwrite_flag = "-y" if plan_item.options.overwrite else "-n"
    base_input = [
        str(ffmpeg_path),
        "-hide_banner",
        overwrite_flag,
        *build_input_acceleration_args(plan_item),
        "-i",
        str(source_path),
    ]

    video_args = build_video_args(plan_item, extra_args=extra_video_args)
    audio_args = build_audio_args(plan_item)
    subtitle_args = build_subtitle_args(plan_item)
    common_output_args = build_common_output_args(plan_item)

    if plan_item.options.two_pass and plan_item.encoder_info.supports_two_pass:
        passlog = passlog_prefix(workdir, plan_item.source_path, stage)
        pass1_video_args = build_video_args(
            plan_item,
            extra_args=extra_video_args,
            muxing_to_null=True,
        )
        pass1 = (
            base_input
            + ["-map", "0:v:0"]
            + pass1_video_args
            + ["-an", "-sn", "-dn", "-pass", "1", "-passlogfile", str(passlog), "-f", "null", _null_sink()]
        )
        pass2 = (
            base_input
            + ["-map", "0:v:0"]
            + audio_args
            + subtitle_args
            + video_args
            + common_output_args
            + ["-pass", "2", "-passlogfile", str(passlog), str(final_output)]
        )
        return [pass1, pass2], passlog

    cmd = (
        base_input
        + ["-map", "0:v:0"]
        + audio_args
        + subtitle_args
        + video_args
        + common_output_args
        + [str(final_output)]
    )
    return [cmd], None
