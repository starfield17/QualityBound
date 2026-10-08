#!/usr/bin/env python3
"""Verify frame slicing/order against independent decoded-frame hashes."""

from __future__ import annotations

import argparse
from fractions import Fraction
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ffmpeg.segmented import build_concat_command, build_shot_commands, concat_manifest  # noqa: E402
from core.models import BackendChoice, CodecChoice, EncodeOptions, EncodePlanItem, EncoderInfo, ShotRange  # noqa: E402


def hashes(ffmpeg: Path, path: Path) -> list[str]:
    result = subprocess.check_output([str(ffmpeg), "-v", "error", "-i", str(path), "-map", "0:v:0",
                                     "-an", "-pix_fmt", "yuv420p", "-fps_mode", "passthrough", "-f", "framemd5", "-"], text=True)
    return [line.rsplit(",", 1)[-1].strip() for line in result.splitlines() if line and not line.startswith("#")]


def verify(ffmpeg: Path, ffprobe: Path, source: Path, workdir: Path) -> dict:
    ffmpeg, ffprobe, source = ffmpeg.resolve(), ffprobe.resolve(), source.resolve()
    if not all(path.is_file() for path in (ffmpeg, ffprobe, source)):
        raise ValueError("Provide explicit existing media and tools.")
    data = json.loads(subprocess.check_output([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_streams",
                                              "-show_frames", "-show_entries", "stream=r_frame_rate:frame=best_effort_timestamp_time",
                                              "-of", "json", str(source)], text=True))
    fps = float(Fraction(data["streams"][0]["r_frame_rate"]))
    frames, origin = len(data["frames"]), float(data["frames"][0]["best_effort_timestamp_time"])
    # Deliberately include one-frame segments and cuts inside moving content.
    cuts = sorted({0, 1, frames // 2, min(frames, frames // 2 + 1), frames - 1, frames})
    ranges = [ShotRange(a, b) for a, b in zip(cuts, cuts[1:]) if b > a]
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="frame-oracle-", dir=workdir) as directory:
        root = Path(directory)
        item = EncodePlanItem(source, root / "joined.mkv", None,
                              EncoderInfo(CodecChoice.HEVC, BackendChoice.CPU, "ffv1", False, None),
                              EncodeOptions(encoder_preset=None, copy_subtitles=False), target_video_bitrate_bps=1000000)
        paths = []
        for index, shot in enumerate(ranges):
            output = root / f"shot-{index}.mkv"
            for command in build_shot_commands(ffmpeg, item, shot, fps, origin, output, root / f"pass-{index}"):
                subprocess.run(command, check=True, capture_output=True)
            paths.append(output)
        manifest = root / "segments.ffconcat"
        manifest.write_text(concat_manifest(paths, [shot.frame_count / fps for shot in ranges]), encoding="utf-8")
        subprocess.run(build_concat_command(ffmpeg, manifest, item.output_path), check=True, capture_output=True)
        expected, observed = hashes(ffmpeg, source), hashes(ffmpeg, item.output_path)
        if expected != observed:
            mismatch = next((i for i, (a, b) in enumerate(zip(expected, observed)) if a != b), min(len(expected), len(observed)))
            raise RuntimeError(f"Decoded frame identity/order differs at frame {mismatch}: {len(expected)} versus {len(observed)} frames.")
    return {"passed": True, "frames": frames, "fps": fps, "origin": origin, "cuts": cuts,
            "ffmpeg_version": subprocess.check_output([str(ffmpeg), "-version"], text=True).splitlines()[0]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.ffmpeg, args.ffprobe, args.source, args.workdir)))


if __name__ == "__main__":
    main()
