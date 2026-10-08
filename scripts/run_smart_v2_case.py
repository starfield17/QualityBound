#!/usr/bin/env python3
"""Explicit-tool v2 evaluation with complete scoring and bounded ABR baselines."""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import json
import math
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.encoding.executor import execute_plan_item  # noqa: E402
from core.encoding.segmented import validate_scores  # noqa: E402
from core.ffmpeg.commands import build_encode_commands  # noqa: E402
from core.ffmpeg.encoders import list_available_encoders, resolve_encoder  # noqa: E402
from core.ffmpeg.probe import probe_media_info  # noqa: E402
from core.models import (  # noqa: E402
    AnalysisProfileName, AudioMode, BackendChoice, CodecChoice, CompressionMode, ContainerChoice,
    EncodeOptions, EncodePlanItem, QualityUnreachablePolicy, SegmentedAnalysisResult,
    ShotRange, SizeBlockedPolicy, SmartAlgorithm,
)
from core.smart.v1.bitrate import calculate_smart_bitrate_budget  # noqa: E402
from core.smart.v1.profiles import bind_analysis_profile  # noqa: E402
from core.smart.v2.runtime import Runtime  # noqa: E402


def child_cpu_seconds() -> float | None:
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def cpu_delta(before: float | None) -> float | None:
    after = child_cpu_seconds()
    return after - before if before is not None and after is not None else None


def metrics(runtime: Runtime, path: Path, reference: SegmentedAnalysisResult, target: float) -> dict:
    fps, _, _, frames = runtime.timeline(path)
    if frames != reference.source_frames or not math.isclose(fps, reference.fps, rel_tol=1e-8):
        raise RuntimeError("Comparison output changed source frame coverage/cadence.")
    quality = copy.deepcopy(reference)
    failed = validate_scores(quality, runtime.score(path, ShotRange(0, frames), fps, reference.source_start_sec), target)
    return {"bytes": path.stat().st_size, "mean_vmaf": quality.final_mean_vmaf,
            "worst_1s_vmaf": quality.final_worst_1s_vmaf, "shot_means": quality.final_shot_means,
            "shot_worst_1s": quality.final_shot_worst_1s, "boundary_worst_1s": quality.final_boundary_worst_1s,
            "quality_passed": not failed, "failed_shots": failed}


def baseline(runtime: Runtime, reference: SegmentedAnalysisResult, *, two_pass: bool, trials: int) -> dict:
    item = copy.deepcopy(runtime.item)
    item.options.compression_mode = CompressionMode.FIXED_BITRATE
    item.options.two_pass = two_pass
    budget = calculate_smart_bitrate_budget(item)
    minimum = budget.min_video_bitrate_bps
    maximum = item.options.max_video_kbps * 1000 or max(budget.max_video_bitrate_bps * 4,
                                                       item.media_info.video_bitrate_bps if item.media_info else 0)
    base = min(maximum, max(minimum, budget.max_video_bitrate_bps))
    points: dict[int, dict] = {}
    initial = sorted({minimum, maximum, *(max(minimum, min(maximum, int(base * factor) // 1000 * 1000))
                                        for factor in (0.25, 0.5, 1, 2, 4))})
    started = time.perf_counter()
    for trial in range(trials):
        if initial:
            rate = initial.pop(0)
        else:
            ordered = sorted(points)
            bracket = next(((a, b) for a, b in zip(ordered, ordered[1:])
                            if not points[a]["quality_passed"] and points[b]["quality_passed"]), None)
            if bracket is None:
                break
            rate = sum(bracket) // 2000 * 1000
            if rate in points or rate in bracket:
                break
        item.target_video_bitrate_bps = rate
        path = runtime.root / f"whole-{'two-pass' if two_pass else 'abr'}-{trial}.mp4"
        runtime.encodes += 1
        commands, passlog = build_encode_commands(runtime.ffmpeg, item, runtime.root, output_path=path)
        for command in commands:
            runtime.run(command, "baseline whole encode")
        if passlog is not None:
            for file in passlog.parent.glob(passlog.name + "*"):
                file.unlink(missing_ok=True)
        points[rate] = {"requested_bitrate_bps": rate, **metrics(runtime, path, reference, item.options.min_vmaf)}
        path.unlink(missing_ok=True)
    passing = [point for point in points.values() if point["quality_passed"]]
    selected = min(passing, key=lambda point: point["bytes"]) if passing else None
    return {"status": "measured" if selected else "no_passing_measured_point", "selected": selected,
            "points": list(points.values()), "elapsed_seconds": time.perf_counter() - started,
            "trial_budget": trials, "global_optimality_claim": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--ffprobe", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--codec", choices=("hevc", "av1"), default="hevc")
    parser.add_argument("--backend", choices=[b.value for b in BackendChoice if b != BackendChoice.AUTO], default="cpu")
    parser.add_argument("--preset")
    parser.add_argument("--profile", choices=[p.value for p in AnalysisProfileName], default="balance")
    parser.add_argument("--target", type=float, default=90)
    parser.add_argument("--max-output-ratio", type=float, default=0.5)
    parser.add_argument("--min-video-kbps", type=int, default=250)
    parser.add_argument("--max-video-kbps", type=int, default=0)
    parser.add_argument("--two-pass", action="store_true")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--baseline-trials", type=int, default=9)
    parser.add_argument("--source-group", required=True, help="Source/provenance group; keep groups disjoint across splits.")
    parser.add_argument("--split", choices=("development", "calibration", "acceptance"), default="development")
    args = parser.parse_args()
    if (not 0 < args.target <= 100 or not 0 < args.max_output_ratio <= 1 or args.baseline_trials < 1
            or args.min_video_kbps < 1 or args.max_video_kbps < 0
            or (args.max_video_kbps and args.max_video_kbps < args.min_video_kbps)):
        parser.error("Invalid quality, size, or trial budget.")
    source, ffmpeg, ffprobe, workdir = (p.resolve() for p in (args.source, args.ffmpeg, args.ffprobe, args.workdir))
    if not all(p.is_file() for p in (source, ffmpeg, ffprobe)):
        parser.error("Source and explicitly supplied tools must exist.")
    workdir.mkdir(parents=True, exist_ok=True)
    report: dict = {"schema": 1, "algorithm": "v2_experimental", "source_group": args.source_group,
                    "split": args.split, "configuration": vars(args), "results": {},
                    "tool_versions": {name: subprocess.check_output([str(path), "-version"], text=True).splitlines()[0]
                                      for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))}}
    try:
        codec, backend = CodecChoice(args.codec), BackendChoice(args.backend)
        encoder = resolve_encoder(codec, backend, list_available_encoders(ffmpeg), ffmpeg)
        if args.two_pass and not encoder.supports_two_pass:
            raise ValueError("Requested bound encoder does not support two-pass mode.")
        options = bind_analysis_profile(EncodeOptions(codec=codec, backend=backend, smart_algorithm=SmartAlgorithm.V2_EXPERIMENTAL,
            min_vmaf=args.target, max_output_ratio=args.max_output_ratio, min_video_kbps=args.min_video_kbps,
            max_video_kbps=args.max_video_kbps, encoder_preset=args.preset or encoder.default_preset, two_pass=args.two_pass,
            audio_mode=AudioMode.AAC, container=ContainerChoice.MP4, overwrite=True,
            size_blocked_policy=SizeBlockedPolicy.ASK, quality_unreachable_policy=QualityUnreachablePolicy.ASK), name=AnalysisProfileName(args.profile))
        item = EncodePlanItem(source, workdir / "v2.mp4", probe_media_info(ffprobe, source), encoder, options, ffprobe_path=ffprobe)
        started, before_cpu = time.perf_counter(), child_cpu_seconds()
        outcome = execute_plan_item(ffmpeg, item, workdir)
        quality = item.segmented_analysis_result
        report["results"]["v2"] = {"success": outcome.success, "needs_decision": outcome.needs_decision,
            "reason": outcome.error_message, "elapsed_seconds": time.perf_counter() - started,
            "child_process_cpu_seconds": cpu_delta(before_cpu), "gpu_seconds": None,
            "analysis": asdict(quality) if quality else None, "actual_bytes": outcome.actual_output_bytes,
            "commands": outcome.commands}
        if quality and quality.predicted_output_bytes and outcome.actual_output_bytes:
            report["results"]["v2"]["prediction_error_ratio"] = outcome.actual_output_bytes / quality.predicted_output_bytes - 1
        if args.compare and quality and quality.shots:
            with (workdir / "comparison.log").open("a", encoding="utf-8") as log:
                runtime = Runtime(ffmpeg, ffprobe, item, workdir, log)
                v1 = copy.deepcopy(item)
                v1.options.smart_algorithm = SmartAlgorithm.V1
                v1.segmented_analysis_result = None
                v1.output_path = workdir / "v1.mp4"
                started, before_cpu = time.perf_counter(), child_cpu_seconds()
                v1_result = execute_plan_item(ffmpeg, v1, workdir / "v1-work")
                native_seconds, native_cpu = time.perf_counter() - started, cpu_delta(before_cpu)
                started, before_cpu = time.perf_counter(), child_cpu_seconds()
                v1_metrics = metrics(runtime, v1.output_path, quality, args.target) if v1_result.success else None
                report["results"]["v1"] = {"success": v1_result.success, "reason": v1_result.error_message,
                                              "elapsed_seconds": native_seconds,
                                              "child_process_cpu_seconds": native_cpu, "gpu_seconds": None,
                                              "posthoc_quality_seconds": time.perf_counter() - started,
                                              "posthoc_quality_cpu_seconds": cpu_delta(before_cpu),
                                              "measured_candidate_points": len(v1.quality_search_result.candidates) if v1.quality_search_result else 0,
                                              "complete_quality": v1_metrics}
                for two_pass in (False, True):
                    key = "whole_two_pass" if two_pass else "whole_abr"
                    if two_pass and not encoder.supports_two_pass:
                        report["results"][key] = {"status": "unsupported_by_bound_encoder"}
                        continue
                    before_times, before_cpu = dict(runtime.times), dict(runtime.cpu_times)
                    before_encodes, before_vmaf = runtime.encodes, runtime.vmaf_calls
                    before_total_cpu = child_cpu_seconds()
                    report["results"][key] = baseline(runtime, quality, two_pass=two_pass, trials=args.baseline_trials)
                    report["results"][key].update(phase_seconds={k: v - before_times.get(k, 0) for k, v in runtime.times.items()},
                        ffmpeg_cpu_seconds=sum(v - before_cpu.get(k, 0) for k, v in runtime.cpu_times.items()),
                        gpu_seconds=None, encodes=runtime.encodes - before_encodes, vmaf_executions=runtime.vmaf_calls - before_vmaf)
                    report["results"][key]["child_process_cpu_seconds"] = cpu_delta(before_total_cpu)
                report["comparison_commands"] = runtime.commands
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        report["error"] = str(exc)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(report, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"result": str(args.result), "v2_success": report["results"].get("v2", {}).get("success", False),
                      "error": report.get("error")}, ensure_ascii=False))
    if report.get("error"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
