#!/usr/bin/env python3
"""Generate reproducible SDR mechanism fixtures without importing Smart code."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess


def recipes(width: int, height: int, fps: int, seed: int) -> dict[str, dict]:
    geometry = f"s={width}x{height}:r={fps}"
    moving = f"testsrc2={geometry}"
    noisy = f"{moving},noise=alls=18:allf=t+u:all_seed={seed}"
    def color(name: str) -> str:
        return f"color=c={name}:{geometry}"
    return {
        "high-low-high": {"clips": [(noisy, 2 * fps), (color("gray"), 4 * fps), (moving, 2 * fps)]},
        "single-shot": {"clips": [(moving, 8 * fps)]},
        "rapid-cuts": {"clips": [(color(name), max(1, fps // 4)) for name in ["red", "blue", "green", "yellow"] * 3]},
        "flash-frame": {"clips": [(color("gray"), 3 * fps), (color("white"), 1), (color("gray"), 3 * fps - 1)]},
        "fade-transition": {"clips": [(moving, 3 * fps), (color("blue"), 3 * fps)], "fade": True},
        "brief-difficulty": {"clips": [(f"{moving},noise=alls=50:allf=t+u:all_seed={seed}:enable='between(n,{3 * fps},{3 * fps + fps // 3})'", 8 * fps)],
                             "event_frames": [3 * fps, 3 * fps + fps // 3 + 1]},
        "auxiliary-streams": {"clips": [(moving, 2 * fps), (color("gray"), 4 * fps), (noisy, 2 * fps)], "auxiliary": True},
    }


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def generate(ffmpeg: Path, ffprobe: Path, output: Path, *, seed: int = 1729,
             width: int = 320, height: int = 180, fps: int = 24,
             split: str = "development", selected: list[str] | None = None) -> dict:
    ffmpeg, ffprobe, output = ffmpeg.resolve(), ffprobe.resolve(), output.resolve()
    if not ffmpeg.is_file() or not ffprobe.is_file():
        raise ValueError("Provide explicit existing FFmpeg and FFprobe executables.")
    if width < 16 or height < 16 or width % 2 or height % 2 or fps < 1:
        raise ValueError("Use even positive geometry and an integer positive frame rate.")
    output.mkdir(parents=True, exist_ok=True)
    versions = {name: subprocess.check_output([str(path), "-version"], text=True).splitlines()[0]
                for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))}
    manifest: dict = {"schema": 1, "purpose": "mechanism-only", "split": split, "seed": seed,
                      "geometry": [width, height], "fps": fps, "tool_versions": versions, "cases": []}
    available = recipes(width, height, fps, seed)
    if selected is not None and set(selected) - available.keys():
        raise ValueError("Unknown fixture name.")
    for name, recipe in available.items():
        if selected is not None and name not in selected:
            continue
        clips = recipe["clips"]
        total_frames = sum(count for _, count in clips) - (fps if recipe.get("fade") else 0)
        duration = total_frames / fps
        command = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        graph = []
        for index, (expression, frames) in enumerate(clips):
            command += ["-f", "lavfi", "-i", expression]
            graph.append(f"[{index}:v]trim=end_frame={frames},setpts=PTS-STARTPTS[v{index}]")
        audio_index = len(clips)
        command += ["-f", "lavfi", "-i", f"sine=frequency={440 + seed % 97}:sample_rate=48000:duration={duration}"]
        if recipe.get("fade"):
            graph.append("[v0][v1]xfade=transition=fade:duration=1:offset=2,format=yuv420p[joined]")
        else:
            graph.append("".join(f"[v{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[joined]")
        graph.append(f"[joined]setpts=N/({fps}*TB)[v]")
        command += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", f"{audio_index}:a",
                    "-frames:v", str(total_frames), "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p",
                    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                    "-c:a", "pcm_s16le", "-metadata", f"title=Smart v2 fixture {name}", "-fps_mode", "passthrough"]
        path = output / f"{name}.mkv"
        intermediate = output / "auxiliary-video.mkv" if recipe.get("auxiliary") else path
        command.append(str(intermediate))
        run(command)
        if recipe.get("auxiliary"):
            subtitles = output / "fixture.srt"
            subtitles.write_text("1\n00:00:00,500 --> 00:00:03,000\nContinuous audio and first subtitle\n\n2\n00:00:04,000 --> 00:00:07,500\nSecond subtitle after shot boundary\n", encoding="utf-8")
            chapters = output / "fixture.ffmetadata"
            chapters.write_text(";FFMETADATA1\ntitle=Smart v2 auxiliary fixture\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=4000\ntitle=First half\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=4000\nEND=8000\ntitle=Second half\n", encoding="utf-8")
            run([str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(intermediate),
                 "-i", str(subtitles), "-f", "ffmetadata", "-i", str(chapters), "-map", "0", "-map", "1:s",
                 "-map_metadata", "2", "-map_chapters", "2", "-c", "copy", str(path)])
            intermediate.unlink()
        probe = json.loads(subprocess.check_output([str(ffprobe), "-v", "error", "-count_frames", "-select_streams", "v:0",
                                                   "-show_entries", "stream=nb_read_frames,r_frame_rate", "-of", "json", str(path)], text=True))
        if int(probe["streams"][0]["nb_read_frames"]) != total_frames:
            raise RuntimeError(f"Fixture {name} has the wrong frame count.")
        boundaries, offset = [], 0
        if not recipe.get("fade"):
            for _, frames in clips[:-1]:
                offset += frames
                boundaries.append(offset)
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest["cases"].append({"id": name, "source": path.name, "source_group": f"synthetic-recipe-{name}",
                                   "sha256": content_hash, "frames": total_frames,
                                   "recipe": recipe, "recipe_boundaries": boundaries, "generator_command": command})
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--split", choices=("development", "calibration", "acceptance"), default="development")
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    generate(args.ffmpeg, args.ffprobe, args.output_dir, seed=args.seed, width=args.width, height=args.height,
             fps=args.fps, split=args.split, selected=args.case)


if __name__ == "__main__":
    main()
