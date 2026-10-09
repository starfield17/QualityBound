"""Cancellable media operations shared by v2 analysis and final verification."""

from __future__ import annotations

from array import array
import copy
import csv
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import subprocess
import time
import threading
from typing import Callable, TextIO

from core.ffmpeg.segmented import build_shot_commands, seek_args
from core.models import EncodePlanItem, OperationCancelledError, ShotRange
from core.progress_events import ProgressCallback
from core.smart.v1.measurement import run_logged
from core.smart.v1.vmaf import (
    PTS_RESET_FILTER, build_cpu_vmaf_command, candidate_encode_metadata,
    select_vmaf_model, validate_vmaf_score, vmaf_thread_budget,
)


class UnsupportedV2(RuntimeError):
    pass


class Runtime:
    def __init__(self, ffmpeg: Path, ffprobe: Path, item: EncodePlanItem, root: Path,
                 log: TextIO, *, cancel_check: Callable[[], bool] | None = None,
                 process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
                 progress_callback: ProgressCallback | None = None,
                 log_callback: Callable[[str], None] | None = None,
                 active_cpu_vmaf_jobs: int = 1) -> None:
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.item = item
        self.root = root
        self.log = log
        self.cancel_check = cancel_check
        self.process_callback = process_callback
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.threads = vmaf_thread_budget(active_cpu_vmaf_jobs)
        self.encodes = 0
        self.vmaf_calls = 0
        self.times: dict[str, float] = {}
        self.cpu_times: dict[str, float] = {}
        self.commands: list[list[str]] = []

    def check(self) -> None:
        if self.cancel_check is not None and self.cancel_check():
            raise OperationCancelledError("Smart v2 cancelled.")

    def emit(self, state: str, message: str, **values: object) -> None:
        from typing import cast
        from core.progress_events import ProgressEvent
        self.check()
        self.log.write(f"[smart v2] {state}: {message}\n")
        self.log.flush()
        if self.log_callback is not None:
            self.log_callback(message)
        if self.progress_callback is not None:
            self.progress_callback(cast(ProgressEvent, {
                "stage": "analysis", "state": state, "message": message,
                "file_path": str(self.item.source_path), "file_name": self.item.source_path.name, **values,
            }))

    def run(self, command: list[str], phase: str) -> None:
        self.check()
        is_ffmpeg = command[0] == str(self.ffmpeg)
        if is_ffmpeg:
            command = list(command)
            command.insert(1, "-benchmark")
            if "-loglevel" in command:
                command[command.index("-loglevel") + 1] = "info"
        self.commands.append(command)
        started = time.perf_counter()
        log_start = self.log.tell()
        stop = threading.Event()
        cancelled = threading.Event()
        watcher: threading.Thread | None = None
        current_process: subprocess.Popen[str] | None = None

        def process_changed(proc: subprocess.Popen[str] | None) -> None:
            nonlocal watcher, current_process
            if proc is None and current_process is not None:
                if current_process.stdout is not None:
                    current_process.stdout.close()
                current_process = None
            elif proc is not None:
                current_process = proc
            if proc is not None and self.cancel_check is not None:
                def watch() -> None:
                    while not stop.wait(0.1):
                        if self.cancel_check is not None and self.cancel_check() and proc.poll() is None:
                            cancelled.set()
                            try:
                                proc.terminate()
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                                proc.wait()
                            except OSError:
                                pass
                            return
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
            if self.process_callback is not None:
                self.process_callback(proc)
        try:
            run_logged(command, self.log, cancel_check=self.cancel_check,
                       process_callback=process_changed, phase=phase)
        finally:
            stop.set()
            if watcher is not None:
                watcher.join(timeout=4)
            self.times[phase] = self.times.get(phase, 0.0) + time.perf_counter() - started
            self.log.flush()
            if is_ffmpeg and isinstance(getattr(self.log, "name", None), str):
                with Path(self.log.name).open(encoding="utf-8") as handle:
                    handle.seek(log_start)
                    matches = re.findall(r"utime=([\d.]+)s stime=([\d.]+)s", handle.read())
                if matches:
                    user, system = matches[-1]
                    self.cpu_times[phase] = self.cpu_times.get(phase, 0.0) + float(user) + float(system)
            if cancelled.is_set():
                raise OperationCancelledError("Smart v2 cancelled.")

    def probe(self, path: Path, *, frames: bool = False, packets: bool = False, extradata: bool = False) -> dict:
        destination = self.root / "probe.json"
        command = [str(self.ffprobe), "-v", "error", "-err_detect", "explode", "-show_streams", "-show_format", "-show_chapters"]
        if frames:
            command += ["-select_streams", "v:0", "-show_frames",
                        "-show_entries", "frame=best_effort_timestamp_time,key_frame:stream:format"]
        if packets:
            command += ["-show_packets", "-show_entries", "packet=stream_index,pts_time,duration_time,size:stream:format:chapter"]
        if extradata:
            command += ["-show_data", "-select_streams", "v:0"]
        self.run(command + ["-of", "json", "-o", str(destination), str(path)], "v2 media probe")
        payload = json.loads(destination.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid media probe result.")
        return payload

    def timeline(self, path: Path) -> tuple[float, str, float, int]:
        data = self.probe(path, frames=True)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            raise UnsupportedV2("Smart v2 requires a video stream.")
        try:
            rational = Fraction(video["r_frame_rate"])
            fps = float(rational)
            timestamps = [float(frame["best_effort_timestamp_time"]) for frame in data["frames"]]
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise UnsupportedV2("Smart v2 requires a fully measurable CFR timeline.") from exc
        if not timestamps or not math.isfinite(fps) or fps <= 0:
            raise UnsupportedV2("Smart v2 requires positive CFR cadence and decoded frames.")
        origin = timestamps[0]
        tolerance = max(0.0011, 0.01 / fps)  # Container timestamp quantization, not a VFR conversion.
        if any(not math.isfinite(t) or abs(t - (origin + i / fps)) > tolerance
               for i, t in enumerate(timestamps)) or any(b <= a for a, b in zip(timestamps, timestamps[1:])):
            raise UnsupportedV2("Smart v2 does not support variable frame rate or discontinuous timestamps.")
        return fps, str(rational), origin, len(timestamps)

    def encode(self, shot: ShotRange, rate: int, fps: float, origin: float, output: Path) -> None:
        clone = copy.deepcopy(self.item)
        clone.target_video_bitrate_bps = rate
        passlog = output.with_suffix(".pass")
        self.encodes += 1
        try:
            for command in build_shot_commands(self.ffmpeg, clone, shot, fps, origin, output, passlog):
                self.run(command, "v2 shot encode")
            data = self.probe(output, frames=True)
            frames = data.get("frames", [])
            if len(frames) != shot.frame_count or not frames or not frames[0].get("key_frame"):
                raise RuntimeError("Shot output is incomplete or does not start with an independently decodable frame.")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        finally:
            for path in output.parent.glob(passlog.name + "*"):
                path.unlink(missing_ok=True)

    def video_bytes(self, path: Path) -> int:
        data = self.probe(path, packets=True)
        video_index = next(s["index"] for s in data["streams"] if s.get("codec_type") == "video")
        return sum(int(p["size"]) for p in data.get("packets", []) if p.get("stream_index") == video_index)

    def score(self, distorted: Path, reference: ShotRange, fps: float, origin: float) -> array:
        media = self.item.media_info
        assert media is not None
        model = select_vmaf_model(media, self.item.options.viewing_context)
        csv_path = self.root / "vmaf.csv"
        command = build_cpu_vmaf_command(
            self.ffmpeg, distorted_path=distorted, reference_path=self.item.source_path,
            model_spec=model, encode_metadata=candidate_encode_metadata(media, self.item.options.pix_fmt),
            log_name=str(csv_path.resolve()), n_threads=self.threads, n_subsample=1,
        )
        inputs = [i for i, value in enumerate(command) if value == "-i"]
        command[inputs[1]:inputs[1]] = seek_args(reference, fps, origin)
        graph_index = command.index("-filter_complex") + 1
        graph = command[graph_index].replace("log_fmt=json", "log_fmt=csv")
        graph = graph.replace(PTS_RESET_FILTER, f"setpts=N/({fps:.12g}*TB)")
        graph = graph.replace("[0:v]", f"[0:v]trim=end_frame={reference.frame_count},")
        graph = graph.replace("[1:v]", f"[1:v]trim=end_frame={reference.frame_count},")
        command[graph_index] = graph
        self.vmaf_calls += 1
        self.run(command, "v2 VMAF")
        scores = array("d")
        with csv_path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            names = {name.lower(): name for name in reader.fieldnames or []}
            score_key = names.get("vmaf") or names.get("vmaf_score")
            frame_key = names.get("framenum") or names.get("frame")
            if score_key is None or frame_key is None:
                raise RuntimeError("VMAF CSV lacks frame numbers or quality scores.")
            for index, row in enumerate(reader):
                self.check()
                if int(row[frame_key]) != index:
                    raise RuntimeError("VMAF frame coverage is not contiguous.")
                scores.append(validate_vmaf_score(float(row[score_key]), model))
        if len(scores) != reference.frame_count:
            raise RuntimeError(f"VMAF covered {len(scores)} frames; expected {reference.frame_count}.")
        return scores

    def stream_signature(self, path: Path) -> tuple:
        data = self.probe(path, extradata=True)
        stream = next(s for s in data["streams"] if s.get("codec_type") == "video")
        signature = tuple(stream.get(key) for key in (
            "codec_name", "profile", "width", "height", "pix_fmt", "sample_aspect_ratio",
            "r_frame_rate", "time_base", "color_range", "color_space", "color_transfer", "color_primaries", "level",
        ))
        return (*signature, decoder_configuration(stream))


def decoder_configuration(stream: dict) -> tuple[bytes, ...]:
    """Compare decoder headers, excluding x265's bitrate-dependent encoder SEI."""
    dump = stream.get("extradata", "")
    data = bytes.fromhex("".join(line.split(":", 1)[1].split("  ", 1)[0].replace(" ", "")
                                 for line in dump.splitlines() if ":" in line))
    if not data:
        raise RuntimeError("Shot has no measurable decoder configuration.")
    if stream.get("codec_name") != "hevc":
        return (data,)
    if len(data) < 23 or data[0] != 1:
        raise RuntimeError("Unsupported HEVC decoder configuration record.")
    parameters = []
    cursor = 23
    for _ in range(data[22]):
        if cursor + 3 > len(data):
            raise RuntimeError("Truncated HEVC decoder array header.")
        kind = data[cursor] & 63
        count = int.from_bytes(data[cursor + 1:cursor + 3], "big")
        cursor += 3
        for _ in range(count):
            if cursor + 2 > len(data):
                raise RuntimeError("Truncated HEVC decoder parameter length.")
            length = int.from_bytes(data[cursor:cursor + 2], "big")
            cursor += 2
            if cursor + length > len(data):
                raise RuntimeError("Truncated HEVC decoder parameter array.")
            if kind in (32, 33, 34):
                parameters.append(data[cursor:cursor + length])
            cursor += length
    if len(parameters) < 3:
        raise RuntimeError("HEVC decoder configuration lacks VPS/SPS/PPS.")
    return tuple(parameters)
