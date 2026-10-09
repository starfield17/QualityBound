#!/usr/bin/env python3
"""Bounded real CPU Smart evaluation case runner.

Executes a single SMART evaluation case against real video media using pure CPU
software decode and encoding, records execution timings and structured phase
counters, performs full-file validation encode and full-frame VMAF evaluation,
and optionally performs an oracle bitrate sweep with bracket refinement.

CLI compatible with evaluate_smart.py manifest placeholders:
{source}, {ffmpeg}, {ffprobe}, {case_id}, {case_dir}, {result_path}.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

# -----------------------------------------------------------------------------
# 1. Early sys.path handling for baseline comparison checkout imports
# -----------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_bootstrap = argparse.ArgumentParser(add_help=False)
_bootstrap.add_argument("--implementation-root", type=Path, default=_REPO_ROOT)
_implementation_root = _bootstrap.parse_known_args()[0].implementation_root.resolve()
if not (_implementation_root / "core/smart/v1/workflow.py").is_file():
    raise SystemExit(f"Invalid implementation checkout: {_implementation_root}")
sys.path.insert(0, str(_implementation_root))

# -----------------------------------------------------------------------------
# 2. Core imports
# -----------------------------------------------------------------------------
from core.ffmpeg.commands import build_encode_commands  # noqa: E402
from core.ffmpeg.probe import probe_media_info  # noqa: E402
from core.ffmpeg.subprocess import noninteractive_run_kwargs  # noqa: E402
from core.models import (  # noqa: E402
    BackendChoice,
    CodecChoice,
    CompressionMode,
    ConstraintFailureKind,
    ContainerChoice,
    DecodeAcceleration,
    EncodeOptions,
    EncodePlanItem,
    EncoderInfo,
    QualitySearchResult,
    QualitySearchStatus,
)
from core.smart.v1.bitrate import calculate_smart_bitrate_budget  # noqa: E402
from core.smart.v1.evaluation import EvaluationDataError, calculate_case_metrics  # noqa: E402
from core.smart.v1.profiles import bind_analysis_profile, parse_analysis_profile_name  # noqa: E402
from core.smart.v1.receipts import load_analysis_receipt  # noqa: E402
from core.smart.v1.runtime import AnalysisDecodePolicy  # noqa: E402
import core.smart.v1.runtime as _smart_runtime  # noqa: E402
import core.smart.v1.workflow as _smart_workflow  # noqa: E402
from core.smart.v1.vmaf import (  # noqa: E402
    MAX_VMAF_THREADS,
    VmafEncodeMetadata,
    VmafModelSpec,
    build_cpu_vmaf_command,
    candidate_encode_metadata,
    select_vmaf_model,
    validate_vmaf_score,
    vmaf_thread_budget,
)


# -----------------------------------------------------------------------------
# 3. CPU Software Decode & Window Cache Ablation Enforcements
# -----------------------------------------------------------------------------
def enforce_software_decode() -> None:
    """Enforce CPU software decode policy at the runner call boundary."""
    orig_build = _smart_runtime.build_analysis_execution_plan

    def wrapped_build_plan(*args: Any, **kwargs: Any) -> _smart_runtime.AnalysisExecutionPlan:
        kwargs["decode_policy"] = AnalysisDecodePolicy.SOFTWARE
        return orig_build(*args, **kwargs)

    _smart_runtime.build_analysis_execution_plan = wrapped_build_plan
    _smart_workflow.build_analysis_execution_plan = wrapped_build_plan


def apply_disable_window_cache_patch() -> None:
    """Ablation patch: discard candidate measurement cache in runner without prod flags."""
    import core.smart.v1.measurement as smart_measurement
    import core.smart.v1.session as smart_session

    orig_score_candidate = smart_measurement.score_candidate

    def patched_score_candidate(*args: Any, **kwargs: Any) -> Any:
        kwargs["measurement_cache"] = None
        kwargs["force_remeasure"] = True
        return orig_score_candidate(*args, **kwargs)

    smart_measurement.score_candidate = patched_score_candidate
    smart_session.score_candidate = patched_score_candidate

    orig_loopback = smart_measurement.score_candidate_loopback

    def patched_score_candidate_loopback(*args: Any, **kwargs: Any) -> Any:
        kwargs["measurement_cache"] = None
        kwargs["force_remeasure"] = True
        return orig_loopback(*args, **kwargs)

    smart_measurement.score_candidate_loopback = patched_score_candidate_loopback
    smart_session.score_candidate_loopback = patched_score_candidate_loopback


# -----------------------------------------------------------------------------
# 4. Constant FPS & CFR/VFR Validation
# -----------------------------------------------------------------------------
def validate_constant_fps(
    ffprobe_path: Path,
    source_path: Path,
) -> tuple[float, int]:
    """Validate CFR and return fps and decoded frame count from one timeline scan.

    Fails clear if variable frame rate (VFR) is detected.
    """
    cmd = [
        str(ffprobe_path),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate,avg_frame_rate,duration,nb_frames",
        "-of",
        "json",
        str(source_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False, **noninteractive_run_kwargs())
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed to probe stream metadata for {source_path}: {res.stderr.strip()}")

    try:
        payload = json.loads(res.stdout)
        stream = payload.get("streams", [{}])[0]
    except (json.JSONDecodeError, IndexError) as exc:
        raise RuntimeError(f"Failed to parse ffprobe JSON for {source_path}: {exc}") from exc

    def parse_fps_str(val: Any) -> float | None:
        if not val or val in ("0/0", "N/A"):
            return None
        if "/" in str(val):
            n, d = str(val).split("/", 1)
            try:
                d_f = float(d)
                return float(n) / d_f if d_f > 0 else None
            except ValueError:
                return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    r_fps = parse_fps_str(stream.get("r_frame_rate"))
    avg_fps = parse_fps_str(stream.get("avg_frame_rate"))

    if r_fps is None and avg_fps is None:
        raise RuntimeError(f"Could not determine video frame rate for: {source_path}")

    # Validate the entire timeline, including late frame-rate changes.
    cmd_frames = [
        str(ffprobe_path),
        "-v",
        "error",
        "-threads",
        "4",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=best_effort_timestamp_time,pkt_pts_time",
        "-of",
        "json",
        str(source_path),
    ]
    res_frames = subprocess.run(cmd_frames, capture_output=True, text=True, check=False, **noninteractive_run_kwargs())
    if res_frames.returncode != 0:
        raise RuntimeError(f"Full timeline probe failed: {res_frames.stderr[-1000:]}")
    f_data = json.loads(res_frames.stdout)
    timestamps = [float(f["best_effort_timestamp_time"]) for f in f_data.get("frames", [])]
    if len(timestamps) < 2 or any(not math.isfinite(t) for t in timestamps):
        raise RuntimeError("Full timeline frame timestamps are missing or invalid")

    if len(timestamps) >= 3:
        deltas = [right - left for left, right in zip(timestamps, timestamps[1:])]
        mean_delta = sum(deltas) / len(deltas)
        max_delta = max(deltas)
        min_delta = min(deltas)
        if min_delta <= 0 or (max_delta - min_delta) > max(0.002, mean_delta * 0.02):
            raise RuntimeError(
                f"Source video '{source_path.name}' is variable frame rate (VFR): "
                f"delta range [{min_delta:.4f}s, {max_delta:.4f}s], mean {mean_delta:.4f}s. "
                "Accurate temporal oracle requires constant frame rate validation."
            )

    if r_fps is not None and avg_fps is not None:
        if abs(r_fps - avg_fps) > 0.05 and abs(r_fps - avg_fps) / max(r_fps, avg_fps) > 0.01:
            raise RuntimeError(
                f"Source video '{source_path.name}' has mismatched frame rates: "
                f"r_frame_rate={r_fps:.3f}, avg_frame_rate={avg_fps:.3f}. "
                "VFR detected; temporal oracle fails clear."
            )
        return r_fps, len(timestamps)

    return float(r_fps or avg_fps or 30.0), len(timestamps)


# -----------------------------------------------------------------------------
# 5. Full-Frame VMAF Metric Computation
# -----------------------------------------------------------------------------
def align_cfr_command(command: list[str], fps: float) -> list[str]:
    """Pair corresponding CFR frames despite container timestamp quantization."""
    aligned = list(command)
    index = aligned.index("-filter_complex") + 1
    aligned[index] = aligned[index].replace("setpts=PTS-STARTPTS", f"setpts=N/({fps:.12g}*TB)")
    # Bound decoder/filter queues independently of libvmaf's worker count.
    for index in reversed([i for i, value in enumerate(aligned) if value == "-i"]):
        aligned[index:index] = ["-threads", "2"]
    aligned[1:1] = ["-filter_complex_threads", "1"]
    return aligned


def decoded_frame_count(probe: Path, source: Path) -> int:
    result = subprocess.run(
        [str(probe), "-v", "error", "-threads", "4", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "json", str(source)],
        capture_output=True, text=True, check=True, **noninteractive_run_kwargs(),
    )
    frames = int(json.loads(result.stdout)["streams"][0]["nb_read_frames"])
    if frames <= 0:
        raise RuntimeError("Source contains no decoded video frames")
    return frames


def require_complete_encode(ffmpeg_path: Path, output: Path, expected_frames: int) -> None:
    probe = ffmpeg_path.with_name("ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe")
    frames = decoded_frame_count(probe, output)
    if abs(frames - expected_frames) > 1:
        raise RuntimeError("Encoded frame count does not cover the complete source")


def compute_full_vmaf_metrics(
    vmaf_json_path: Path,
    model_spec: VmafModelSpec,
    fps: float,
    expected_frames: int | None = None,
) -> tuple[float, float, float]:
    """Independently compute (mean_vmaf, worst_1s, gate_score) from libvmaf JSON.

    Gate score uses the standard Smart v2 pooling formula: min(mean, worst_1s + 4.0).
    """
    if not vmaf_json_path.is_file():
        raise RuntimeError(f"VMAF log file not found at: {vmaf_json_path}")
    try:
        data = json.loads(vmaf_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Failed to read VMAF JSON at {vmaf_json_path}: {exc}") from exc

    raw_frames = data.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        raise RuntimeError(f"VMAF JSON contains no per-frame scores: {vmaf_json_path}")

    scores: list[float] = []
    frame_numbers: list[int] = []
    for idx, frame in enumerate(raw_frames):
        if not isinstance(frame, dict):
            raise RuntimeError(f"Invalid frame entry at index {idx} in {vmaf_json_path}")
        metrics = frame.get("metrics")
        if not isinstance(metrics, dict) or "vmaf" not in metrics:
            raise RuntimeError(f"Missing VMAF metric at frame {idx} in {vmaf_json_path}")
        score = validate_vmaf_score(float(metrics["vmaf"]), model_spec)
        scores.append(score)
        frame_numbers.append(int(frame.get("frameNum", idx)))

    mean_vmaf = sum(scores) / len(scores)
    total_frames = len(scores)
    if expected_frames is not None and abs(total_frames - expected_frames) > 1:
        raise RuntimeError("VMAF frame count does not cover the complete source")

    if frame_numbers != list(range(total_frames)):
        raise RuntimeError("Ground truth requires contiguous full-frame VMAF scores")
    window_size = min(total_frames, max(1, math.ceil(fps)))
    rolling = sum(scores[:window_size])
    lowest = rolling
    for index in range(window_size, total_frames):
        rolling += scores[index] - scores[index - window_size]
        lowest = min(lowest, rolling)
    worst_1s = lowest / window_size

    gate_score = min(mean_vmaf, worst_1s + 4.0)
    return mean_vmaf, worst_1s, gate_score


def compute_segmented_vmaf_metrics(
    ffmpeg_path: Path, distorted_path: Path, reference_path: Path,
    model_spec: VmafModelSpec, encode_metadata: VmafEncodeMetadata,
    fps: float, expected_frames: int, workdir: Path,
    *, segment_duration_sec: float = 30.0, overlap_sec: float = 1.0,
) -> tuple[float, float, float]:
    """Score long CFR inputs in bounded sequential segments and merge by frame."""
    all_scores: list[float] = []
    distorted_path = distorted_path.resolve()
    reference_path = reference_path.resolve()
    workdir = workdir.resolve()
    segment_frames = max(1, round(segment_duration_sec * fps))
    overlap_frames = max(0, round(overlap_sec * fps))
    step = max(1, segment_frames - overlap_frames)
    for start_frame in range(0, expected_frames, step):
        count = min(segment_frames, expected_frames - start_frame)
        start = start_frame / fps
        duration = count / fps
        log_path = workdir / f"seg-vmaf-{start_frame:09d}.json"
        command = build_cpu_vmaf_command(
            ffmpeg_path, distorted_path=distorted_path, reference_path=reference_path,
            model_spec=model_spec, encode_metadata=encode_metadata,
            log_name=str(log_path), n_threads=2, n_subsample=1,
        )
        # Apply the same trim to both inputs before the filter graph.
        input_indices = [index for index, value in enumerate(command) if value == "-i"]
        for input_index in reversed(input_indices):
            command[input_index:input_index] = ["-ss", f"{start:.6f}", "-t", f"{duration:.6f}"]
        command = align_cfr_command(command, fps)
        result = subprocess.run(command, check=False, cwd=workdir, capture_output=True, text=True, **noninteractive_run_kwargs())
        if result.returncode != 0:
            raise RuntimeError(f"Segmented VMAF failed at frame {start_frame}: {result.stderr[-500:]}")
        data = json.loads(log_path.read_text(encoding="utf-8"))
        scores = [validate_vmaf_score(float(frame["metrics"]["vmaf"]), model_spec) for frame in data["frames"]]
        if start_frame:
            scores = scores[min(overlap_frames, len(scores)):]
        all_scores.extend(scores)
        if len(all_scores) >= expected_frames:
            break
    if len(all_scores) != expected_frames:
        raise RuntimeError("Segmented VMAF did not cover the complete source")
    mean = sum(all_scores) / len(all_scores)
    window = max(1, round(fps))
    rolling = sum(all_scores[:window])
    lowest = rolling
    for index in range(window, len(all_scores)):
        rolling += all_scores[index] - all_scores[index-window]
        lowest = min(lowest, rolling)
    worst = lowest / window
    return mean, worst, min(mean, worst + 4.0)


# -----------------------------------------------------------------------------
# 6. Structured Phase Log Parser & Cache Hit Counters
# -----------------------------------------------------------------------------
def parse_smart_log(
    log_path: Path,
    receipt_data: Any | None = None,
) -> dict[str, Any]:
    """Parse phase seconds, cache hits, and the 5 SMART counters from log text."""
    phase_seconds: dict[str, float] = {}
    phase_counts: dict[str, int] = {}
    ref_cache_hits = 0
    meas_cache_hits = 0
    scout_windows_from_header: int | None = None
    holdout_measurements = 0

    if log_path.is_file():
        try:
            content = log_path.read_text(encoding="utf-8")
        except OSError:
            content = ""

        for line in content.splitlines():
            sline = line.strip()
            if sline.startswith("[smart phase] "):
                payload_str = sline[len("[smart phase] "):].strip()
                try:
                    pdata = json.loads(payload_str)
                    phase = str(pdata.get("phase", "unknown"))
                    sec = float(pdata.get("seconds", 0.0))
                    phase_seconds[phase] = phase_seconds.get(phase, 0.0) + sec
                    phase_counts[phase] = phase_counts.get(phase, 0) + 1
                except (json.JSONDecodeError, ValueError):
                    pass
            elif sline.startswith("[smart timing] reference cache hit:"):
                ref_cache_hits += 1
            elif sline.startswith("[smart timing] measurement cache hit:"):
                meas_cache_hits += 1
            elif sline.startswith("scout_windows="):
                try:
                    scout_windows_from_header = int(sline.split("=", 1)[1])
                except ValueError:
                    pass
            elif sline.startswith("holdout="):
                holdout_measurements += 1

    # Scout windows
    if scout_windows_from_header is not None:
        scout_windows = scout_windows_from_header
    elif receipt_data is not None and hasattr(receipt_data, "scout_windows"):
        scout_windows = len(receipt_data.scout_windows)
    else:
        scout_windows = phase_counts.get("content complexity scout", 0)

    # Quality encodes: each window encode executed
    quality_encodes = (
        phase_counts.get("candidate encode", 0)
        + phase_counts.get("candidate pass 2", 0)
        + phase_counts.get("loopback score", 0)
    )

    # VMAF measurements
    vmaf_measurements = (
        phase_counts.get("VMAF scoring", 0)
        + phase_counts.get("loopback score", 0)
    )

    # Size calibration encodes
    size_cal_encodes = phase_counts.get("representative size calibration", 0)
    if receipt_data is not None and hasattr(receipt_data, "size_calibration_windows"):
        size_cal_encodes = max(size_cal_encodes, len(receipt_data.size_calibration_windows))

    # Holdouts

    return {
        "counts": {
            "scout_windows": max(0, scout_windows),
            "quality_encodes": max(0, quality_encodes),
            "vmaf_measurements": max(0, vmaf_measurements),
            "holdout_measurements": max(0, holdout_measurements),
            "size_calibration_encodes": max(0, size_cal_encodes),
        },
        "phase_seconds": phase_seconds,
        "cache_hits": {
            "reference_cache_hits": ref_cache_hits,
            "measurement_cache_hits": meas_cache_hits,
            "total_cache_hits": ref_cache_hits + meas_cache_hits,
        },
    }


# -----------------------------------------------------------------------------
# 7. Oracle Bitrate Sweep with Monotonic Bracket Refinement
# -----------------------------------------------------------------------------
@dataclasses.dataclass(slots=True)
class OraclePoint:
    bitrate_bps: int
    mean_vmaf: float
    worst_1s: float
    gate_score: float
    passed: bool
    output_bytes: int


def _round_bps(value: int | float) -> int:
    return max(1_000, int(round(value / 1_000.0)) * 1_000)


def run_oracle_search(
    *,
    ffmpeg_path: Path,
    item: EncodePlanItem,
    workdir: Path,
    target_min_vmaf: float,
    fps: float,
    model_spec: VmafModelSpec,
    encode_metadata: VmafEncodeMetadata,
    evaluated_cache: dict[int, OraclePoint],
    executed_commands: list[list[str]],
    max_coarse_points: int = 8,
    max_refinements: int = 3,
    expected_frames: int | None = None,
) -> tuple[int | None, dict[str, Any]]:
    """Execute full-file bitrate sweep plus bracket refinement (<=8 coarse, 3 refinements).

    Returns (oracle_minimum_bitrate_bps, oracle_bracket_dict).
    No fabricated exact float is returned; reports grid approximation.
    """
    budget = calculate_smart_bitrate_budget(item)
    min_bps = budget.min_video_bitrate_bps
    ceiling_bps = max(item.media_info.video_bitrate_bps if item.media_info else 0, budget.max_video_bitrate_bps)
    if item.options.max_video_kbps > 0:
        ceiling_bps = min(ceiling_bps, int(item.options.max_video_kbps) * 1_000)
    if ceiling_bps <= min_bps:
        ceiling_bps = min_bps + 500_000

    def evaluate_point(bitrate_bps: int) -> OraclePoint:
        bitrate_bps = _round_bps(bitrate_bps)
        if bitrate_bps in evaluated_cache:
            return evaluated_cache[bitrate_bps]

        out_video = workdir / f"oracle_{bitrate_bps}.mp4"
        vmaf_json = workdir / f"oracle_vmaf_{bitrate_bps}.json"

        # Build encode commands
        enc_item = dataclasses.replace(
            item,
            target_video_bitrate_bps=bitrate_bps,
            output_path=out_video,
        )
        commands, _passlog = build_encode_commands(ffmpeg_path, enc_item, workdir, output_path=out_video)
        for cmd in commands:
            executed_commands.append(cmd)
            run_res = subprocess.run(cmd, check=False, cwd=workdir, capture_output=True, text=True, **noninteractive_run_kwargs())
            if run_res.returncode != 0:
                raise RuntimeError(
                    f"Oracle encode failed at {bitrate_bps} bps (code {run_res.returncode}):\n{run_res.stderr[-500:]}"
                )

        if not out_video.is_file() or out_video.stat().st_size == 0:
            raise RuntimeError(f"Oracle encode produced empty output at {bitrate_bps} bps: {out_video}")

        output_bytes = out_video.stat().st_size
        if expected_frames is not None:
            require_complete_encode(ffmpeg_path, out_video, expected_frames)

        # Build full-frame VMAF command
        vmaf_cmd = build_cpu_vmaf_command(
            ffmpeg_path,
            distorted_path=out_video,
            reference_path=item.source_path,
            model_spec=model_spec,
            encode_metadata=encode_metadata,
            log_name=str(vmaf_json),
            n_threads=min(2, MAX_VMAF_THREADS, vmaf_thread_budget()),
            n_subsample=1,
        )
        vmaf_cmd = align_cfr_command(vmaf_cmd, fps)
        executed_commands.append(vmaf_cmd)
        vmaf_res = subprocess.run(vmaf_cmd, check=False, cwd=workdir, capture_output=True, text=True, **noninteractive_run_kwargs())
        if vmaf_res.returncode != 0:
            raise RuntimeError(
                f"Oracle VMAF scoring failed at {bitrate_bps} bps (code {vmaf_res.returncode}):\n{vmaf_res.stderr[-500:]}"
            )

        mean_v, worst_1s, gate = compute_full_vmaf_metrics(
            vmaf_json, model_spec, fps, expected_frames=expected_frames,
        )
        pt = OraclePoint(
            bitrate_bps=bitrate_bps,
            mean_vmaf=mean_v,
            worst_1s=worst_1s,
            gate_score=gate,
            passed=(gate >= target_min_vmaf),
            output_bytes=output_bytes,
        )
        evaluated_cache[bitrate_bps] = pt
        return pt

    # Coarse sweep: up to max_coarse_points
    k_points = min(max_coarse_points, 8)
    if k_points <= 0:
        coarse_bitrates = []
    elif k_points < 2:
        coarse_bitrates = [min_bps]
    else:
        coarse_bitrates = [
            _round_bps(min_bps + i * (ceiling_bps - min_bps) / (k_points - 1))
            for i in range(k_points)
        ]
    coarse_bitrates = sorted(set(coarse_bitrates))

    for cbps in coarse_bitrates:
        evaluate_point(cbps)

    # Monotonic bracket resolution
    sorted_pts = [evaluated_cache[b] for b in sorted(evaluated_cache.keys())]
    failing = [pt for pt in sorted_pts if not pt.passed]
    passing = [pt for pt in sorted_pts if pt.passed]

    lower_bps: int | None = None
    upper_bps: int | None = None

    if not passing:
        # All evaluated points failed
        lower_bps = max(pt.bitrate_bps for pt in sorted_pts)
        upper_bps = None
    elif not failing:
        # All evaluated points passed
        lower_bps = None
        upper_bps = min(pt.bitrate_bps for pt in sorted_pts)
    else:
        upper_bps = min(pt.bitrate_bps for pt in passing)
        failing_below = [pt.bitrate_bps for pt in failing if pt.bitrate_bps < upper_bps]
        lower_bps = max(failing_below) if failing_below else None

    # Bracket refinement: up to max_refinements bisection steps
    refinements_done = 0
    while (
        refinements_done < max_refinements
        and lower_bps is not None
        and upper_bps is not None
        and (upper_bps - lower_bps) > 20_000
    ):
        mid_bps = _round_bps((lower_bps + upper_bps) // 2)
        if mid_bps in evaluated_cache or mid_bps in (lower_bps, upper_bps):
            break
        mid_pt = evaluate_point(mid_bps)
        refinements_done += 1
        if mid_pt.passed:
            upper_bps = mid_bps
        else:
            lower_bps = mid_bps

    # Report grid approximation: upper_bps is the lowest tested passing point on grid
    oracle_minimum_bitrate_bps = upper_bps

    oracle_bracket = {
        "method": "tested_grid_with_local_refinement",
        "monotonicity_violated": any(pt.bitrate_bps > upper_bps for pt in failing) if upper_bps is not None else False,
        "lower_bps": lower_bps,
        "upper_bps": upper_bps,
        "lower_gate_score": (evaluated_cache[lower_bps].gate_score if lower_bps in evaluated_cache else None),
        "upper_gate_score": (evaluated_cache[upper_bps].gate_score if upper_bps in evaluated_cache else None),
        "coarse_points_evaluated": len(coarse_bitrates),
        "refinement_evaluations": refinements_done,
        "grid": [
            {
                "bitrate_bps": pt.bitrate_bps,
                "gate_score": pt.gate_score,
                "mean_vmaf": pt.mean_vmaf,
                "worst_1s": pt.worst_1s,
                "passed": pt.passed,
                "output_bytes": pt.output_bytes,
            }
            for pt in [evaluated_cache[b] for b in sorted(evaluated_cache.keys())]
        ],
    }

    return oracle_minimum_bitrate_bps, oracle_bracket


# -----------------------------------------------------------------------------
# 8. Main CLI Argument Parsing & Runner Workflow
# -----------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bounded real CPU Smart evaluation runner with full encode validation and oracle sweep."
    )
    parser.add_argument("--source", type=Path, required=True, help="Input source video file")
    parser.add_argument("--ffmpeg", type=Path, required=True, help="Path to ffmpeg executable")
    parser.add_argument("--ffprobe", type=Path, required=True, help="Path to ffprobe executable")
    parser.add_argument("--result", type=Path, required=True, help="Output measurement JSON path")
    parser.add_argument("--workdir", type=Path, required=True, help="Working directory for runs and temporary files")
    parser.add_argument(
        "--encoder",
        choices=["libx265", "libsvtav1"],
        default="libx265",
        help="Encoder name (libx265|libsvtav1, default: libx265)",
    )
    parser.add_argument("--preset", default=None, help="Encoder preset (default: fast for x265, 6 for SVT-AV1)")
    parser.add_argument(
        "--profile",
        choices=["fast", "balance", "precise"],
        default="balance",
        help="SMART analysis profile (default: balance)",
    )
    parser.add_argument("--min-vmaf", type=float, default=95.0, help="Minimum target VMAF score (default: 95.0)")
    parser.add_argument("--max-video-kbps", type=int, default=0, help="Optional search bitrate ceiling")
    parser.add_argument(
        "--max-output-ratio",
        type=float,
        default=0.8,
        help="Maximum allowed output ratio (default: 0.8)",
    )
    parser.add_argument(
        "--oracle",
        action="store_true",
        help="Enable full-file bitrate sweep plus bracket refinement (<=8 coarse, 3 refinements)",
    )
    parser.add_argument(
        "--disable-window-cache",
        action="store_true",
        help="Ablation: discard candidate measurement cache in runner without prod flags",
    )
    parser.add_argument(
        "--implementation-root",
        type=Path,
        default=None,
        help="Path to baseline repo checkout to load via sys.path prior to core imports",
    )
    parser.add_argument("--case-id", default=None, help="Optional case ID string")
    parser.add_argument("--case-dir", type=Path, default=None, help="Optional case directory")
    parser.add_argument(
        "--two-pass",
        action="store_true",
        help="Use two-pass encoding if supported by encoder (default: one pass)",
    )
    return parser


def _require_tools(ffmpeg: Path, ffprobe: Path) -> tuple[Path, Path]:
    ffmpeg_res = ffmpeg.expanduser().resolve()
    ffprobe_res = ffprobe.expanduser().resolve()
    if not ffmpeg_res.is_file():
        raise FileNotFoundError(f"ffmpeg binary not found at: {ffmpeg_res}")
    if not ffprobe_res.is_file():
        raise FileNotFoundError(f"ffprobe binary not found at: {ffprobe_res}")
    if ffmpeg_res.parent != ffprobe_res.parent:
        raise ValueError(
            f"ffprobe must be located in the same directory as ffmpeg ({ffmpeg_res.parent}), "
            f"got: {ffprobe_res.parent}"
        )
    return ffmpeg_res, ffprobe_res


def _tool_versions(ffmpeg: Path, ffprobe: Path) -> dict[str, str]:
    def first_line(cmd: list[str]) -> str:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, **noninteractive_run_kwargs())
            lines = (res.stdout or res.stderr).splitlines()
            return lines[0].strip() if lines else "unknown"
        except Exception as exc:
            return f"error: {exc}"

    return {
        "ffmpeg": first_line([str(ffmpeg), "-version"]),
        "ffprobe": first_line([str(ffprobe), "-version"]),
    }


def run_smart_case(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    total_start = time.perf_counter()
    executed_commands: list[list[str]] = []

    # 1. Tool verification
    try:
        ffmpeg_path, ffprobe_path = _require_tools(args.ffmpeg, args.ffprobe)
    except Exception as exc:
        sys.stderr.write(f"Tool validation error: {exc}\n")
        return 1

    source_path = args.source.expanduser().resolve()
    if not source_path.is_file():
        sys.stderr.write(f"Source video not found: {source_path}\n")
        return 1

    case_id = args.case_id or source_path.stem
    workdir = (args.case_dir or args.workdir).expanduser().resolve()
    if source_path.is_relative_to(workdir):
        raise ValueError("Source must be outside the evaluation working directory")
    if workdir.exists() and any(workdir.iterdir()):
        raise ValueError("Evaluation requires an empty working directory for a cold run")
    workdir.mkdir(parents=True, exist_ok=True)
    result_path = args.result.expanduser().resolve()
    if result_path == source_path:
        raise ValueError("Result path must not overwrite the source")
    result_path.parent.mkdir(parents=True, exist_ok=True)

    tool_version_info = _tool_versions(ffmpeg_path, ffprobe_path)

    # 2. Enforce CPU software decode & cache ablation if requested
    enforce_software_decode()
    if args.disable_window_cache:
        apply_disable_window_cache_patch()

    # 3. Validate constant FPS / fail clear on VFR
    try:
        validated_fps, source_frames = validate_constant_fps(ffprobe_path, source_path)
    except Exception as exc:
        sys.stderr.write(f"Frame rate validation failed: {exc}\n")
        return 1

    # 4. Probe media information
    try:
        media_info = probe_media_info(ffprobe_path, source_path)
    except Exception as exc:
        sys.stderr.write(f"Media probe failed for {source_path}: {exc}\n")
        return 1

    # 5. Build EncoderInfo & EncodeOptions
    encoder_name = args.encoder
    if encoder_name == "libx265":
        codec = CodecChoice.HEVC
        encoder_info = EncoderInfo(
            codec=codec,
            backend=BackendChoice.CPU,
            encoder_name="libx265",
            supports_two_pass=True,
            default_preset="fast",
        )
    elif encoder_name == "libsvtav1":
        codec = CodecChoice.AV1
        encoder_info = EncoderInfo(
            codec=codec,
            backend=BackendChoice.CPU,
            encoder_name="libsvtav1",
            supports_two_pass=False,
            default_preset="6",
        )
    else:
        sys.stderr.write(f"Unsupported encoder: {encoder_name}\n")
        return 1

    options = EncodeOptions(
        codec=codec,
        compression_mode=CompressionMode.SMART,
        backend=BackendChoice.CPU,
        decode_acceleration=DecodeAcceleration.SOFTWARE,
        min_vmaf=args.min_vmaf,
        max_output_ratio=args.max_output_ratio,
        max_video_kbps=args.max_video_kbps,
        encoder_preset=args.preset or encoder_info.default_preset,
        two_pass=bool(args.two_pass and encoder_info.supports_two_pass),
        container=ContainerChoice.MP4,
        overwrite=True,
        copy_subtitles=False,
        copy_external_subtitles=False,
    )
    profile_enum = parse_analysis_profile_name(args.profile)
    options = bind_analysis_profile(options, name=profile_enum)

    # 6. Construct probed EncodePlanItem
    analysis_out = workdir / f"{case_id}_analysis.mp4"
    plan_item = EncodePlanItem(
        source_path=source_path,
        output_path=analysis_out,
        media_info=media_info,
        encoder_info=encoder_info,
        options=options,
    )

    # 7. Run SMART quality analysis
    smart_log_path = workdir / "smart_analysis.log"
    analysis_start = time.perf_counter()
    search_result: QualitySearchResult
    try:
        search_result = _smart_workflow.analyze_quality(
            ffmpeg_path=ffmpeg_path,
            item=plan_item,
            workdir=workdir,
            log_path=smart_log_path,
        )
    except Exception as exc:
        sys.stderr.write(f"Smart analysis failed: {exc}\n")
        return 1
    analysis_wall_seconds = time.perf_counter() - analysis_start
    if search_result.status in (QualitySearchStatus.FAILED, QualitySearchStatus.UNSUPPORTED):
        raise RuntimeError(f"Smart evaluation could not complete: {search_result.reason}")

    # 8. Parse analysis log for structured phase seconds, cache hits, counters
    receipt = load_analysis_receipt(workdir, search_result.measurement_fingerprint)
    log_info = parse_smart_log(smart_log_path, receipt)
    counts = log_info["counts"]
    phase_seconds = log_info["phase_seconds"]
    cache_hits = log_info["cache_hits"]

    # 9. Determine selected bitrate or quality candidate under size blocked
    smart_passed = (search_result.status == QualitySearchStatus.FOUND)
    smart_size_blocked = (search_result.failure_kind == ConstraintFailureKind.SIZE_BLOCKED)
    budget = calculate_smart_bitrate_budget(plan_item)
    max_output_bytes = budget.max_output_bytes

    bitrate_to_encode: int | None = None
    if smart_passed:
        bitrate_to_encode = search_result.selected_video_bitrate_bps
    elif smart_size_blocked:
        # Check if there is a selectable quality candidate that met min_vmaf
        quality_candidates = [
            c for c in search_result.candidates
            if c.min_vmaf >= args.min_vmaf and c.video_bitrate_bps > 0
        ]
        if quality_candidates:
            # Pick lowest bitrate passing candidate to verify if it actually exceeds size
            best_q = min(quality_candidates, key=lambda c: c.video_bitrate_bps)
            bitrate_to_encode = best_q.video_bitrate_bps

    # 10. Perform full encode and full-frame VMAF evaluation
    final_encode_wall_seconds: float | None = None
    full_encode_output_bytes: int | None = None
    full_vmaf_wall_seconds: float | None = None
    ground_truth_passed: bool | None = None
    vmaf_details: dict[str, float] | None = None
    evaluated_oracle_cache: dict[int, OraclePoint] = {}

    model_spec = select_vmaf_model(media_info, options.viewing_context)
    encode_metadata = candidate_encode_metadata(media_info, options.pix_fmt)

    if bitrate_to_encode is not None:
        full_encode_path = workdir / f"full_encode_{bitrate_to_encode}.mp4"
        vmaf_json_path = workdir / f"full_vmaf_{bitrate_to_encode}.json"

        # Full encode
        encode_item = dataclasses.replace(
            plan_item,
            target_video_bitrate_bps=bitrate_to_encode,
            output_path=full_encode_path,
        )
        encode_commands, _passlog = build_encode_commands(
            ffmpeg_path,
            encode_item,
            workdir,
            output_path=full_encode_path,
        )

        encode_start = time.perf_counter()
        for cmd in encode_commands:
            executed_commands.append(cmd)
            res_enc = subprocess.run(
                cmd,
                check=False,
                cwd=workdir,
                capture_output=True,
                text=True,
                **noninteractive_run_kwargs(),
            )
            if res_enc.returncode != 0:
                raise RuntimeError(
                    f"Full encode failed for bitrate {bitrate_to_encode} (code {res_enc.returncode}):\n"
                    f"{res_enc.stderr[-500:]}"
                )
        final_encode_wall_seconds = time.perf_counter() - encode_start

        if not full_encode_path.is_file() or full_encode_path.stat().st_size == 0:
            raise RuntimeError(f"Full encode produced empty output: {full_encode_path}")

        full_encode_output_bytes = full_encode_path.stat().st_size
        require_complete_encode(ffmpeg_path, full_encode_path, source_frames)

        vmaf_start = time.perf_counter()
        if media_info.duration > 120.0:
            mean_v, worst_1s, gate = compute_segmented_vmaf_metrics(
                ffmpeg_path, full_encode_path, source_path, model_spec, encode_metadata,
                validated_fps, source_frames, workdir,
            )
        else:
            vmaf_cmd = align_cfr_command(build_cpu_vmaf_command(
                ffmpeg_path, distorted_path=full_encode_path, reference_path=source_path,
                model_spec=model_spec, encode_metadata=encode_metadata,
                log_name=str(vmaf_json_path), n_threads=2, n_subsample=1,
            ), validated_fps)
            executed_commands.append(vmaf_cmd)
            res_vmaf = subprocess.run(vmaf_cmd, check=False, cwd=workdir, capture_output=True, text=True, **noninteractive_run_kwargs())
            if res_vmaf.returncode != 0:
                raise RuntimeError(f"Full-frame VMAF failed (code {res_vmaf.returncode}):\n{res_vmaf.stderr[-500:]}")
            mean_v, worst_1s, gate = compute_full_vmaf_metrics(vmaf_json_path, model_spec, validated_fps, expected_frames=source_frames)
        full_vmaf_wall_seconds = time.perf_counter() - vmaf_start
        ground_truth_passed = (gate >= args.min_vmaf)
        vmaf_details = {
            "mean": mean_v,
            "worst_1s": worst_1s,
            "gate_score": gate,
        }

        # Cache this point for oracle sweep if needed
        assert ground_truth_passed is not None and full_encode_output_bytes is not None
        evaluated_oracle_cache[bitrate_to_encode] = OraclePoint(
            bitrate_bps=bitrate_to_encode,
            mean_vmaf=mean_v,
            worst_1s=worst_1s,
            gate_score=gate,
            passed=ground_truth_passed,
            output_bytes=full_encode_output_bytes,
        )

    # 11. Oracle Sweep (optional)
    oracle_minimum_bitrate_bps: int | None = None
    oracle_bracket: dict[str, Any] | None = None
    oracle_wall_seconds: float | None = None

    if args.oracle:
        oracle_start = time.perf_counter()
        oracle_minimum_bitrate_bps, oracle_bracket = run_oracle_search(
            ffmpeg_path=ffmpeg_path,
            item=plan_item,
            workdir=workdir,
            target_min_vmaf=args.min_vmaf,
            fps=validated_fps,
            model_spec=model_spec,
            encode_metadata=encode_metadata,
            evaluated_cache=evaluated_oracle_cache,
            executed_commands=executed_commands,
            max_coarse_points=8,
            max_refinements=3,
            expected_frames=source_frames,
        )
        oracle_wall_seconds = time.perf_counter() - oracle_start

    total_wall_seconds = time.perf_counter() - total_start

    # 12. Assemble measurement record
    selected_video_bitrate_bps = bitrate_to_encode if smart_passed else None

    actual_output_ratio: float | None = None
    if full_encode_output_bytes is not None and budget.source_bytes > 0:
        actual_output_ratio = full_encode_output_bytes / budget.source_bytes

    measurement: dict[str, Any] = {
        "schema_version": 1,
        "case_id": case_id,
        "source": str(source_path),
        "analysis_wall_seconds": analysis_wall_seconds,
        "final_encode_wall_seconds": final_encode_wall_seconds,
        "full_vmaf_wall_seconds": full_vmaf_wall_seconds,
        "oracle_wall_seconds": oracle_wall_seconds,
        "total_wall_seconds": total_wall_seconds,
        "smart_passed": smart_passed,
        "ground_truth_passed": ground_truth_passed,
        "smart_size_blocked": smart_size_blocked,
        "selected_video_bitrate_bps": selected_video_bitrate_bps,
        "validated_video_bitrate_bps": bitrate_to_encode,
        "oracle_minimum_bitrate_bps": oracle_minimum_bitrate_bps,
        "oracle_bracket": oracle_bracket,
        "full_encode_output_bytes": full_encode_output_bytes,
        "max_output_bytes": max_output_bytes,
        "predicted_output_bytes": search_result.predicted_output_bytes,
        "predicted_output_ratio": search_result.predicted_output_ratio,
        "actual_output_ratio": actual_output_ratio,
        "min_vmaf": args.min_vmaf,
        "vmaf": vmaf_details,
        "counts": counts,
        "phase_seconds": phase_seconds,
        "cache_hits": cache_hits,
        "decisions": {
            "status": search_result.status.value,
            "failure_kind": search_result.failure_kind.value if search_result.failure_kind else None,
            "reason": search_result.reason,
            "selected_video_bitrate_bps": search_result.selected_video_bitrate_bps,
            "required_video_bitrate_bps": search_result.required_video_bitrate_bps,
        },
        "tool_version": tool_version_info,
        "implementation_root": str(_implementation_root),
        "window_cache_disabled": args.disable_window_cache,
        "commands": executed_commands,
    }

    # Verify that the measurement conforms to calculate_case_metrics
    try:
        calculate_case_metrics(measurement)
    except EvaluationDataError as exc:
        sys.stderr.write(f"Generated measurement failed contract check: {exc}\n")
        return 1

    # Write output to result_path
    result_path.write_text(json.dumps(measurement, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run_smart_case(argv)


if __name__ == "__main__":
    sys.exit(main())
