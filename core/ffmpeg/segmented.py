"""Frame-bounded shot commands and video-copy assembly."""

from __future__ import annotations

from pathlib import Path
import math

from core.ffmpeg.commands import (
    build_audio_args, build_common_output_args, build_input_acceleration_args,
    build_subtitle_args, build_video_args,
)
from core.models import ContainerChoice, EncodePlanItem, ShotRange


def common_hevc_level(item: EncodePlanItem) -> str:
    """One Main-tier envelope for every x265 shot, independent of size/quality policy.

    HEVC Annex A picture/rate/CPB limits. Actual encoder and concat preflight
    still validate the requested configuration; this is not a decoder claim.
    """
    media = item.media_info
    if media is None or not media.width or not media.height or not media.fps or media.duration <= 0:
        raise ValueError("A common HEVC configuration requires probed geometry and cadence.")
    maximum = item.options.max_video_kbps * 1000 or max(
        media.video_bitrate_bps, math.ceil(item.source_path.stat().st_size * 8 / media.duration)
    ) * 4
    maximum = max(maximum, item.options.min_video_kbps * 1000)
    limits = (
        ("1", 36864, 552960, 128000, 350000),
        ("2", 122880, 3686400, 1500000, 1500000),
        ("2.1", 245760, 7372800, 3000000, 3000000),
        ("3", 552960, 16588800, 6000000, 6000000),
        ("3.1", 983040, 33177600, 10000000, 10000000),
        ("4", 2228224, 66846720, 12000000, 12000000),
        ("4.1", 2228224, 133693440, 20000000, 20000000),
        ("5", 8912896, 267386880, 25000000, 25000000),
        ("5.1", 8912896, 534773760, 40000000, 40000000),
        ("5.2", 8912896, 1069547520, 60000000, 60000000),
        ("6", 35651584, 1069547520, 60000000, 60000000),
        ("6.1", 35651584, 2139095040, 120000000, 120000000),
        ("6.2", 35651584, 4278190080, 240000000, 240000000),
    )
    pixels = media.width * media.height
    for level, picture, rate, bitrate, buffer in limits:
        if (pixels <= picture and pixels * media.fps <= rate
                and max(media.width, media.height) ** 2 <= picture * 8
                and maximum * item.options.maxrate_factor <= bitrate
                and maximum * item.options.bufsize_factor <= buffer):
            return level
    raise ValueError("Smart v2 HEVC candidate envelope exceeds the supported Main-tier level limits; set a bitrate ceiling.")


def seek_args(shot: ShotRange, fps: float, source_start: float) -> list[str]:
    # Seek half a cadence before the wanted frame. Accurate input seeking then
    # discards the previous frame without rounding a wanted frame away.
    if shot.start_frame == 0:
        return []
    return ["-seek_timestamp", "1", "-ss", f"{source_start + (shot.start_frame - 0.5) / fps:.9f}"]


def build_shot_commands(ffmpeg: Path, item: EncodePlanItem, shot: ShotRange,
                        fps: float, source_start: float, output: Path,
                        passlog: Path) -> list[list[str]]:
    encoder = item.encoder_info
    if encoder is None or shot.frame_count <= 0:
        raise ValueError("Shot encoding needs an encoder and a nonempty frame range.")
    base = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            *build_input_acceleration_args(item), *seek_args(shot, fps, source_start),
            "-i", str(item.source_path), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-frames:v", str(shot.frame_count), "-vf", f"setpts=N/({fps:.12g}*TB)",
            "-fps_mode", "passthrough", "-map_metadata", "-1", "-map_chapters", "-1"]
    extra: tuple[str, ...] = ()
    if encoder.encoder_name == "libx265":
        extra = ("-x265-params", f"log-level=error:open-gop=0:level-idc={common_hevc_level(item)}:high-tier=0")
    elif encoder.encoder_name in {"hevc_nvenc", "av1_nvenc"}:
        extra = ("-forced-idr", "1")
    video = build_video_args(item, extra)
    if item.options.two_pass:
        if not encoder.supports_two_pass:
            raise ValueError("Bound encoder does not support two-pass shot encoding.")
        import os
        sink = "NUL" if os.name == "nt" else "/dev/null"
        return [base + video + ["-pass", "1", "-passlogfile", str(passlog), "-f", "null", sink],
                base + video + ["-pass", "2", "-passlogfile", str(passlog), str(output)]]
    return [base + video + [str(output)]]


def concat_manifest(paths: list[Path], durations: list[float] | None = None) -> str:
    # ffconcat has its own quoting rules, separate from shell quoting.
    if durations is not None and len(durations) != len(paths):
        raise ValueError("Each concatenated shot needs its frame-derived duration.")
    entries = []
    for index, path in enumerate(paths):
        entry = "file '" + str(path.resolve()).replace("'", "'\\''") + "'\n"
        if durations is not None:
            entry += f"duration {durations[index]:.9f}\n"
        entries.append(entry)
    return "ffconcat version 1.0\n" + "".join(entries)


def build_concat_command(ffmpeg: Path, manifest: Path, output: Path) -> list[str]:
    return [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(manifest), "-map", "0:v:0",
            "-c:v", "copy", "-an", str(output)]


def build_mux_command(ffmpeg: Path, item: EncodePlanItem, video: Path,
                      output: Path, source_start: float) -> list[str]:
    common = build_common_output_args(item)
    # Original metadata/chapters and audio/subtitles are on input 1.
    common = ["1" if value == "0" else value for value in common]
    audio = ["1:a?" if value == "0:a?" else value for value in build_audio_args(item)]
    subtitles = ["1:s?" if value == "0:s?" else value for value in build_subtitle_args(item)]
    tag = ["-tag:v", "hvc1"] if item.options.codec.value == "hevc" and item.options.container == ContainerChoice.MP4 else []
    return [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-copyts", "-i", str(video), "-itsoffset", f"{-source_start:.9f}",
            "-i", str(item.source_path), "-map", "0:v:0", "-c:v", "copy",
            *tag, *audio, *subtitles, *common, "-avoid_negative_ts", "disabled", str(output)]
