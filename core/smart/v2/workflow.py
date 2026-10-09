"""Experimental shot detection, sampled candidate search, and allocation."""

from __future__ import annotations

import math
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable
import uuid

from core.ffmpeg.segmented import build_concat_command, concat_manifest
from core.models import (
    ConstraintFailureKind, EncodePlanItem, OperationCancelledError, QualitySearchStatus, SegmentedAnalysisResult,
    ShotAnalysis, ShotCandidate, ShotRange,
)
from core.progress_events import ProgressCallback
from core.smart.v1.bitrate import calculate_smart_bitrate_budget
from core.ffmpeg.filters import quote_filter_value
from core.smart.v2.optimizer import SETTINGS, allocate, sample_windows, worst_one_second
from core.smart.v2.receipts import file_hash, fingerprint, load, receipt_root, save
from core.smart.v2.runtime import Runtime, UnsupportedV2
from core.smart.v1.vmaf import select_vmaf_model, select_vmaf_runtime


def detect_shots(runtime: Runtime, frames: int) -> list[ShotRange]:
    path = runtime.root / "scenes.txt"
    filters = ("scale=w='min(480,trunc(iw/2)*2)':h='max(2,trunc(ow/dar/2)*2)',"
               "scdet=threshold=10,metadata=mode=print:key=lavfi.scd.score:file="
               f"{quote_filter_value(str(path.resolve()))}")
    runtime.run([str(runtime.ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                 "-i", str(runtime.item.source_path), "-map", "0:v:0", "-an", "-sn", "-vf", filters,
                 "-fps_mode", "passthrough", "-f", "null", "-"], "v2 shot detection")
    cuts = [0]
    frame = -1
    seen = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"frame:\s*(\d+)", line)
        if match:
            frame = int(match[1])
            if frame != seen:
                raise RuntimeError("Scene scan frame coverage is not contiguous.")
            seen += 1
        elif line.startswith("lavfi.scd.score=") and frame > 0:
            score = float(line.split("=", 1)[1])
            if not math.isfinite(score):
                raise RuntimeError("Scene detector returned a non-finite score.")
            if score >= 10:
                cuts.append(frame)
    if seen != frames:
        raise RuntimeError("Scene detector did not cover every source frame.")
    cuts = sorted(set([*cuts, frames]))
    return [ShotRange(left, right) for left, right in zip(cuts, cuts[1:])]


def measure_candidate(runtime: Runtime, result: SegmentedAnalysisResult, analysis: ShotAnalysis,
                      rate: int, assets: Path, *, force: bool = False) -> ShotCandidate:
    if not force:
        cached = next((c for c in analysis.candidates if c.bitrate_bps == rate), None)
        if cached is not None:
            result.measurement_cache_hits += 1
            return cached
    samples = analysis.search_windows
    whole = samples == [analysis.shot]
    mean_sum = 0.0
    frames = 0
    worst = 100.0
    bytes_measured = 0
    artifact: Path | None = None
    for sample in samples:
        path = assets / f"shot-{sample.start_frame}-{sample.end_frame}-{rate}-{uuid.uuid4().hex}.mkv"
        try:
            runtime.encode(sample, rate, result.fps, result.source_start_sec, path)
            scores = runtime.score(path, sample, result.fps, result.source_start_sec)
            mean_sum += sum(scores)
            frames += len(scores)
            worst = min(worst, worst_one_second(scores, result.fps))
            bytes_measured += runtime.video_bytes(path)
            if whole:
                artifact = path
        finally:
            if not whole or artifact is None:
                path.unlink(missing_ok=True)
    predicted = math.ceil(bytes_measured / frames * analysis.shot.frame_count * (1.0 if whole else 1.10))
    candidate = ShotCandidate(rate, mean_sum / frames, worst, predicted, frames,
                              whole_shot=whole, artifact=artifact,
                              artifact_hash=file_hash(artifact) if artifact is not None else None,
                              holdout_verified=whole, full_verified=whole)
    analysis.candidates = [c for c in analysis.candidates if c.bitrate_bps != rate] + [candidate]
    return candidate


def reselect(result: SegmentedAnalysisResult, item: EncodePlanItem) -> SegmentedAnalysisResult:
    media = item.media_info
    assert media is not None
    budget = calculate_smart_bitrate_budget(item)
    audio_bytes = math.ceil(budget.audio_bitrate_bps * (result.source_frames / result.fps) / 8)
    result.max_output_bytes = budget.max_output_bytes
    result.video_budget_bytes = max(0, math.floor(budget.max_output_bytes * 0.98) - audio_bytes)
    if item.segmented_video_budget_bytes is not None:
        result.video_budget_bytes = min(result.video_budget_bytes, item.segmented_video_budget_bytes)
    minimum = item.options.min_video_kbps * 1000
    maximum = item.options.max_video_kbps * 1000
    eligible = [ShotAnalysis(s.shot, s.search_windows, s.holdout_window,
                            [c for c in s.candidates if c.bitrate_bps >= minimum
                             and (maximum == 0 or c.bitrate_bps <= maximum)]) for s in result.shots]
    settings = SETTINGS[item.options.analysis_profile]
    target = float(item.options.min_vmaf)
    diagnostics: dict[str, bool] = {}
    selected = allocate(eligible, target, result.video_budget_bytes, settings.state_limit, diagnostics=diagnostics)
    result.approximate = diagnostics.get("approximate", False)
    result.required_output_ratio = None
    result.best_size_fitting_vmaf = None
    result.failure_kind = None
    if selected is None:
        required = allocate(eligible, target, None, settings.state_limit)
        if required is not None:
            result.failure_kind = ConstraintFailureKind.SIZE_BLOCKED
            result.required_output_ratio = max(budget.max_output_bytes / item.source_path.stat().st_size,
                                               math.ceil((required.video_bytes + audio_bytes) / 0.98) / item.source_path.stat().st_size)
            # Floors track the new target, so test the proposed relaxed target too.
            # A relaxed target changes the local floors too. Find a measured
            # combination at the corresponding target, then validate it again.
            low, high = 0.0, target
            for _ in range(10):
                relaxed = math.floor((low + high) * 5) / 10
                fitting = allocate(eligible, relaxed, result.video_budget_bytes, settings.state_limit)
                if fitting is not None:
                    result.best_size_fitting_vmaf = relaxed
                    low = relaxed
                else:
                    high = relaxed
        else:
            result.failure_kind = (ConstraintFailureKind.MEDIA_BUDGET_TOO_SMALL if result.video_budget_bytes <= 0
                                   else ConstraintFailureKind.QUALITY_UNREACHABLE)
        result.status = QualitySearchStatus.CONSTRAINT_UNSATISFIED
        result.selected = []
        result.reason = "Smart v2 found no feasible measured combination within its candidate/state budget; this does not prove infeasibility."
        return result
    result.selected = list(selected.selected)
    result.predicted_mean_vmaf = selected.mean_vmaf
    result.predicted_output_bytes = math.ceil((selected.video_bytes + audio_bytes) / 0.98)
    result.approximate = selected.approximate
    result.status = QualitySearchStatus.FOUND
    result.reason = None
    return result


def verify_holdouts(runtime: Runtime, result: SegmentedAnalysisResult, assets: Path) -> bool:
    changed = False
    for analysis, candidate in zip(result.shots, result.selected):
        holdout = analysis.holdout_window
        if holdout is None or candidate.holdout_verified:
            continue
        path = runtime.root / "holdout.mkv"
        runtime.encode(holdout, candidate.bitrate_bps, result.fps, result.source_start_sec, path)
        scores = runtime.score(path, holdout, result.fps, result.source_start_sec)
        observed = runtime.video_bytes(path)
        count = len(scores)
        candidate.mean_vmaf = (candidate.mean_vmaf * candidate.measured_frames + sum(scores)) / (candidate.measured_frames + count)
        candidate.measured_frames += count
        candidate.worst_1s_vmaf = min(candidate.worst_1s_vmaf, worst_one_second(scores, result.fps))
        candidate.predicted_video_bytes = max(candidate.predicted_video_bytes,
                                             math.ceil(observed / count * analysis.shot.frame_count * 1.10))
        candidate.holdout_verified = True
        changed = True
    return changed


def preflight(runtime: Runtime, result: SegmentedAnalysisResult, minimum: int, maximum: int) -> None:
    count = min(result.source_frames, max(1, math.ceil(result.fps / 2)))
    shot = ShotRange(0, count)
    paths = [runtime.root / "preflight-low.mkv", runtime.root / "preflight-high.mkv"]
    try:
        runtime.encode(shot, minimum, result.fps, result.source_start_sec, paths[0])
        runtime.encode(shot, maximum, result.fps, result.source_start_sec, paths[1])
        if runtime.stream_signature(paths[0]) != runtime.stream_signature(paths[1]):
            raise RuntimeError("Bound encoder produces incompatible stream configurations at different bitrates.")
        manifest = runtime.root / "preflight.ffconcat"
        manifest.write_text(concat_manifest(paths, [count / result.fps] * 2), encoding="utf-8")
        output = runtime.root / "preflight.mkv"
        runtime.run(build_concat_command(runtime.ffmpeg, manifest, output), "v2 concatenation preflight")
        _, _, _, decoded = runtime.timeline(output)
        if decoded != count * 2:
            raise RuntimeError("Concatenation preflight did not preserve frames.")
    except OperationCancelledError:
        raise
    except (subprocess.CalledProcessError, RuntimeError, OSError) as exc:
        raise UnsupportedV2(f"Smart v2 encode/concatenation preflight failed for the bound encoder: {exc}") from exc


def analyze_segmented_quality(ffmpeg_path: Path, item: EncodePlanItem, workdir: Path, log_path: Path,
                              *, progress_callback: ProgressCallback | None = None,
                              cancel_check: Callable[[], bool] | None = None,
                              process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
                              log_callback: Callable[[str], None] | None = None,
                              active_cpu_vmaf_jobs: int = 1) -> SegmentedAnalysisResult:
    encoder, media = item.encoder_info, item.media_info
    if encoder is None or media is None or item.ffprobe_path is None:
        raise ValueError("Smart v2 requires probed media, a bound encoder, and an explicit FFprobe path.")
    result = SegmentedAnalysisResult(QualitySearchStatus.FAILED, encoder.encoder_name, encoder.backend)
    if media.color_transfer in {"smpte2084", "arib-std-b67"}:
        result.status, result.reason = QualitySearchStatus.UNSUPPORTED, "Smart v2 supports SDR only."
        return result
    support = select_vmaf_runtime(ffmpeg_path, select_vmaf_model(media, item.options.viewing_context))
    if not support.runnable:
        result.status, result.reason = QualitySearchStatus.UNSUPPORTED, support.error_message
        return result
    try:
        result.measurement_fingerprint = fingerprint(ffmpeg_path, item.ffprobe_path, item)
    except ValueError as exc:
        result.status, result.reason = QualitySearchStatus.UNSUPPORTED, str(exc)
        return result
    except OSError as exc:
        result.status, result.reason = QualitySearchStatus.FAILED, str(exc)
        return result
    root = receipt_root(workdir, result.measurement_fingerprint)
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    existing_assets = set(assets.iterdir())
    loaded = load(root, result)
    settings = SETTINGS[item.options.analysis_profile]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="session-", dir=root) as directory, log_path.open("a", encoding="utf-8") as log:
        runtime = Runtime(ffmpeg_path, item.ffprobe_path, item, Path(directory), log,
                          cancel_check=cancel_check, process_callback=process_callback,
                          progress_callback=progress_callback, log_callback=log_callback,
                          active_cpu_vmaf_jobs=active_cpu_vmaf_jobs)
        try:
            if not loaded:
                result.fps, result.fps_rational, result.source_start_sec, result.source_frames = runtime.timeline(item.source_path)
                runtime.emit("scouting", "Smart v2: scanning every frame for shot boundaries.")
                shots = detect_shots(runtime, result.source_frames)
                for shot in shots:
                    windows, holdout = sample_windows(shot, result.fps, settings)
                    result.shots.append(ShotAnalysis(shot, windows, holdout))
            budget = calculate_smart_bitrate_budget(item)
            base = max(budget.min_video_bitrate_bps, budget.max_video_bitrate_bps)
            ceiling = item.options.max_video_kbps * 1000 or max(base * 4, media.video_bitrate_bps)
            ceiling = max(budget.min_video_bitrate_bps, ceiling)
            preflight(runtime, result, budget.min_video_bitrate_bps, ceiling)
            runtime.emit("searching", f"Smart v2: {len(result.shots)} shots; up to {settings.candidate_limit} initial candidates per shot.",
                         scout_count=len(result.shots), candidate_limit=settings.candidate_limit,
                         measurement_budget=sum(len(s.search_windows) * settings.candidate_limit for s in result.shots),
                         segmented_analysis_result=result)
            for index, analysis in enumerate(result.shots):
                initial = [max(budget.min_video_bitrate_bps, base // 2), base, min(ceiling, base * 2)]
                for rate in sorted(set(min(ceiling, rate) // 1000 * 1000 for rate in initial)):
                    if len(analysis.candidates) >= settings.candidate_limit:
                        break
                    measure_candidate(runtime, result, analysis, max(1000, rate), assets)
                while len(analysis.candidates) < settings.candidate_limit:
                    ordered = sorted(analysis.candidates, key=lambda c: c.bitrate_bps)
                    target = item.options.min_vmaf
                    bracket = next(((a, b) for a, b in zip(ordered, ordered[1:])
                                    if a.mean_vmaf < target <= b.mean_vmaf), None)
                    if bracket:
                        rate = ((bracket[0].bitrate_bps + bracket[1].bitrate_bps) // 2000) * 1000
                    elif max(c.mean_vmaf for c in ordered) < target:
                        rate = min(ceiling, ordered[-1].bitrate_bps * 2)
                    else:
                        rate = max(budget.min_video_bitrate_bps, ordered[0].bitrate_bps // 2000 * 1000)
                    if any(c.bitrate_bps == rate for c in ordered):
                        break
                    measure_candidate(runtime, result, analysis, rate, assets)
                ordered = sorted(analysis.candidates, key=lambda c: c.bitrate_bps)
                if any(a.mean_vmaf > b.mean_vmaf + 0.2 for a, b in zip(ordered, ordered[1:])):
                    point = min(ordered, key=lambda c: abs(c.mean_vmaf - item.options.min_vmaf))
                    measure_candidate(runtime, result, analysis, point.bitrate_bps, assets, force=True)
                runtime.emit("searching", f"Smart v2: measured shot {index + 1}/{len(result.shots)}.",
                             candidate_index=index + 1, candidate_limit=len(result.shots))
            reselect(result, item)
            # Each selected unverified candidate is checked at most once; reselection
            # can expose another candidate without reusing its former holdout result.
            for _ in range(sum(len(s.candidates) for s in result.shots) + 1):
                if not result.success or not verify_holdouts(runtime, result, assets):
                    break
                reselect(result, item)
            for round_index in range(settings.refinement_limit):
                if result.success or result.failure_kind != ConstraintFailureKind.QUALITY_UNREACHABLE:
                    break
                changed = False
                for analysis in result.shots:
                    best = max(analysis.candidates, key=lambda c: c.bitrate_bps)
                    if best.mean_vmaf >= item.options.min_vmaf and best.worst_1s_vmaf >= max(0, item.options.min_vmaf - 8):
                        continue
                    rate = min(ceiling, math.ceil(best.bitrate_bps * 1.5 / 1000) * 1000)
                    if rate > best.bitrate_bps:
                        measure_candidate(runtime, result, analysis, rate, assets)
                        changed = True
                if not changed:
                    break
                result.holdout_refinement_rounds = round_index + 1
                reselect(result, item)
                for _ in range(sum(len(s.candidates) for s in result.shots) + 1):
                    if not result.success or not verify_holdouts(runtime, result, assets):
                        break
                    reselect(result, item)
            save(root, result)
        except UnsupportedV2 as exc:
            result.status, result.reason = QualitySearchStatus.UNSUPPORTED, str(exc)
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            if isinstance(exc, OperationCancelledError):
                for path in set(assets.iterdir()) - existing_assets:
                    path.unlink(missing_ok=True)
                raise
            result.status, result.reason = QualitySearchStatus.FAILED, str(exc)
        finally:
            result.candidate_encodes += runtime.encodes
            result.vmaf_executions += runtime.vmaf_calls
            result.phase_seconds.update(runtime.times)
            result.phase_cpu_seconds.update(runtime.cpu_times)
            result.resource_cpu_seconds = sum(runtime.cpu_times.values()) or None
    return result
