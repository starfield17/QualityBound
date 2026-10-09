"""Experimental segmented execution, complete quality gate, and atomic publication."""

from __future__ import annotations

from array import array
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Callable
import uuid

from core.ffmpeg.segmented import build_concat_command, build_mux_command, concat_manifest
from core.media.paths import log_file_path
from core.models import (
    ConstraintFailureKind, ConstraintPolicy, EncodePlanItem, EncodeResult,
    OperationCancelledError, QualitySearchStatus, SegmentedAnalysisResult, ShotRange,
)
from core.progress_events import ProgressCallback
from core.smart.v1.decisions import resolve_analysis_policy
from core.smart.v1.concurrency import analysis_concurrency_limit, analysis_slot
from core.smart.v2.optimizer import SETTINGS, worst_one_second
from core.smart.v2.receipts import file_hash, fingerprint, receipt_root, save
from core.smart.v2.runtime import Runtime, UnsupportedV2
from core.smart.v2.workflow import analyze_segmented_quality, measure_candidate, reselect, verify_holdouts

from .item_results import _copy_external_subtitles_for_result, _size_miss_output_path
from .process import _emit, _emit_progress


def analyze_segmented_plan_item(ffmpeg: Path, item: EncodePlanItem, workdir: Path, *,
                                queue_index: int = 1, queue_total: int = 1,
                                log_callback: Callable[[str], None] | None = None,
                                progress_callback: ProgressCallback | None = None,
                                cancel_check: Callable[[], bool] | None = None,
                                process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
                                extra_progress_context: dict[str, object] | None = None,
                                constraint_policy: ConstraintPolicy | None = None,
                                active_cpu_vmaf_jobs: int = 1) -> EncodeResult | None:
    log_path = log_file_path(workdir, item.source_path, "analysis-v2")
    try:
        quality = analyze_segmented_quality(ffmpeg, item, workdir, log_path,
                                           progress_callback=progress_callback, cancel_check=cancel_check,
                                           log_callback=log_callback,
                                           process_callback=process_callback, active_cpu_vmaf_jobs=active_cpu_vmaf_jobs)
    except OperationCancelledError:
        raise
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        encoder = item.encoder_info
        quality = SegmentedAnalysisResult(QualitySearchStatus.FAILED, encoder.encoder_name if encoder else "",
                                          encoder.backend if encoder else item.options.backend, reason=str(exc))
    quality, skip_origin = resolve_analysis_policy(ffmpeg, item, quality, constraint_policy)
    item.segmented_analysis_result = quality
    item.quality_search_result = None
    context = dict(extra_progress_context or {})
    context.update(file_path=str(item.source_path), file_name=item.source_path.name,
                   current=queue_index, total=queue_total)
    if quality.success:
        _emit_progress(progress_callback, stage="analysis", state="analysis_finished",
                       segmented_analysis_result=quality, **context)
        return None
    skipped = skip_origin is not None
    result = EncodeResult(item.source_path, item.output_path, False, log_path=log_path,
                          error_message=quality.reason, segmented_analysis_result=quality,
                          skipped=skipped, skip_origin=skip_origin,
                          needs_decision=quality.status == QualitySearchStatus.CONSTRAINT_UNSATISFIED and not skipped,
                          effective_min_vmaf=item.options.min_vmaf,
                          effective_max_output_ratio=item.options.max_output_ratio)
    _emit(log_callback, quality.reason or "Smart v2 analysis failed.")
    _emit_progress(progress_callback, stage="analysis", state="skipped" if skipped else (
        "needs_decision" if result.needs_decision else "failed"), message=result.error_message,
        segmented_analysis_result=quality, **context)
    return result


def validate_scores(result: SegmentedAnalysisResult, scores: array, target: float) -> list[int]:
    if len(scores) != result.source_frames:
        raise RuntimeError("Final video quality measurement does not cover the complete source.")
    result.final_mean_vmaf = sum(scores) / len(scores)
    result.final_worst_1s_vmaf = worst_one_second(scores, result.fps)
    result.final_shot_means = []
    result.final_shot_worst_1s = []
    result.final_boundary_worst_1s = []
    failed: set[int] = set()
    for index, analysis in enumerate(result.shots):
        shot = analysis.shot
        values = scores[shot.start_frame:shot.end_frame]
        mean = sum(values) / len(values)
        worst = worst_one_second(values, result.fps)
        result.final_shot_means.append(mean)
        result.final_shot_worst_1s.append(worst)
        if mean < max(0, target - 4) or worst < max(0, target - 8):
            failed.add(index)
    length = min(len(scores), max(1, math.ceil(result.fps)))
    prefix = array("d", [0.0])
    running = 0.0
    for score in scores:
        running += score
        prefix.append(running)
    for index, analysis in enumerate(result.shots[:-1]):
        cut = analysis.shot.end_frame
        starts = range(max(0, cut - length + 1), min(cut, len(scores) - length + 1))
        boundary = min(((prefix[start + length] - prefix[start]) / length for start in starts), default=100.0)
        result.final_boundary_worst_1s.append(boundary)
        if boundary < max(0, target - 8):
            failed.update((index, index + 1))
    if result.final_mean_vmaf < target and not failed:
        # Choose the shot with the largest duration-weighted quality deficit.
        index = max(range(len(result.shots)), key=lambda i: (
            (target - result.final_shot_means[i]) * result.shots[i].shot.frame_count
        ))
        failed.add(index)
    return sorted(failed)


def _validate_auxiliary(runtime: Runtime, output: Path, source_start: float) -> None:
    source, final = runtime.probe(runtime.item.source_path, packets=True), runtime.probe(output, packets=True)
    for kind in ("audio", "subtitle"):
        if kind == "subtitle" and not runtime.item.options.copy_subtitles:
            continue
        original = [s for s in source["streams"] if s.get("codec_type") == kind]
        encoded = [s for s in final["streams"] if s.get("codec_type") == kind]
        if len(original) != len(encoded):
            raise RuntimeError(f"Final {kind} stream count differs from the source.")
        if kind == "audio":
            for left, right in zip(original, encoded):
                source_packets = [p for p in source.get("packets", []) if p.get("stream_index") == left["index"] and "pts_time" in p]
                final_packets = [p for p in final.get("packets", []) if p.get("stream_index") == right["index"] and "pts_time" in p]
                if not source_packets:
                    continue
                if not final_packets:
                    raise RuntimeError("Final audio stream has no timestamped packets.")
                source_first = float(source_packets[0]["pts_time"]) - source_start
                final_first = float(final_packets[0]["pts_time"])
                source_end = float(source_packets[-1]["pts_time"]) + float(source_packets[-1].get("duration_time", 0)) - source_start
                final_end = float(final_packets[-1]["pts_time"]) + float(final_packets[-1].get("duration_time", 0))
                tolerance = max(float(source_packets[-1].get("duration_time", 0)),
                                float(final_packets[-1].get("duration_time", 0)),
                                float(final_packets[0].get("duration_time", 0))) + 0.002
                if abs(final_first - source_first) > tolerance or abs(final_end - source_end) > tolerance:
                    raise RuntimeError("Final audio timestamp coverage differs from the source.")
    original_chapters, final_chapters = source.get("chapters", []), final.get("chapters", [])
    if len(original_chapters) != len(final_chapters):
        raise RuntimeError("Final chapter count differs from the source.")
    for original, encoded in zip(original_chapters, final_chapters):
        if (any(abs((float(original[k]) - source_start) - float(encoded[k])) > 0.002 for k in ("start_time", "end_time"))
                or original.get("tags", {}).get("title") != encoded.get("tags", {}).get("title")):
            raise RuntimeError("Final chapter coordinates or titles differ from the source.")


def execute_segmented_item(ffmpeg: Path, item: EncodePlanItem, workdir: Path, *,
                           queue_index: int = 1, queue_total: int = 1,
                           log_callback: Callable[[str], None] | None = None,
                           progress_callback: ProgressCallback | None = None,
                           cancel_check: Callable[[], bool] | None = None,
                           process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
                           extra_progress_context: dict[str, object] | None = None,
                           constraint_policy: ConstraintPolicy | None = None,
                           smart_analysis_validated: bool = False) -> EncodeResult:
    needs_analysis = not smart_analysis_validated
    if smart_analysis_validated and item.segmented_analysis_result is not None and item.ffprobe_path is not None:
        try:
            needs_analysis = item.segmented_analysis_result.measurement_fingerprint != fingerprint(ffmpeg, item.ffprobe_path, item)
        except (ValueError, OSError):
            needs_analysis = True
    if needs_analysis:
        terminal = analyze_segmented_plan_item(ffmpeg, item, workdir, queue_index=queue_index, queue_total=queue_total,
                                               log_callback=log_callback, progress_callback=progress_callback,
                                               cancel_check=cancel_check, process_callback=process_callback,
                                               extra_progress_context=extra_progress_context,
                                               constraint_policy=constraint_policy)
        if terminal is not None:
            return terminal
    quality, encoder, ffprobe = item.segmented_analysis_result, item.encoder_info, item.ffprobe_path
    log_path = log_file_path(workdir, item.source_path, "encode-v2")
    result = EncodeResult(item.source_path, item.output_path, False, log_path=log_path,
                          segmented_analysis_result=quality, effective_min_vmaf=item.options.min_vmaf,
                          effective_max_output_ratio=item.options.max_output_ratio)
    temporary: Path | None = None
    uncommitted_assets: set[Path] = set()
    context = dict(extra_progress_context or {})
    context.update(stage="encode", file_path=str(item.source_path), file_name=item.source_path.name,
                   current=queue_index, total=queue_total)
    try:
        if quality is None or not quality.success or encoder is None or ffprobe is None:
            raise RuntimeError("Smart v2 encoding needs a successful segmented analysis and explicit tools.")
        if quality.encoder_name != encoder.encoder_name or quality.backend != encoder.backend:
            raise RuntimeError("Smart v2 result belongs to a different bound encoder.")
        if quality.measurement_fingerprint != fingerprint(ffmpeg, ffprobe, item):
            raise RuntimeError("Smart v2 measurements no longer match the source, tools, or encoding settings.")
        reselect(quality, item)
        if not quality.success:
            result.needs_decision = True
            result.error_message = quality.reason
            return result
        item.output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = item.output_path.with_name(f".{item.output_path.stem}.smart-v2-{uuid.uuid4().hex}{item.output_path.suffix}")
        root = receipt_root(workdir, quality.measurement_fingerprint)
        assets = root / "assets"
        settings = SETTINGS[item.options.analysis_profile]
        with tempfile.TemporaryDirectory(prefix="encode-", dir=root) as directory, log_path.open("a", encoding="utf-8") as log:
            runtime = Runtime(ffmpeg, ffprobe, item, Path(directory), log, cancel_check=cancel_check,
                              process_callback=process_callback, progress_callback=progress_callback,
                              log_callback=log_callback,
                              active_cpu_vmaf_jobs=analysis_concurrency_limit())
            try:
                with analysis_slot(cancel_check):
                    for _ in range(sum(len(s.candidates) for s in quality.shots) + 1):
                        if not quality.success or not verify_holdouts(runtime, quality, assets):
                            break
                        reselect(quality, item)
                if not quality.success:
                    result.error_message, result.needs_decision = quality.reason, True
                    return result
                for round_index in range(settings.refinement_limit + 1):
                    runtime.check()
                    selected = list(quality.selected)
                    paths: list[Path] = []
                    signature = None
                    for index, (analysis, candidate) in enumerate(zip(quality.shots, selected)):
                        path = candidate.artifact
                        if path is None or not path.is_file() or candidate.artifact_hash != file_hash(path):
                            path = assets / f"final-{analysis.shot.start_frame}-{candidate.bitrate_bps}-{uuid.uuid4().hex}.mkv"
                            uncommitted_assets.add(path)
                            runtime.encode(analysis.shot, candidate.bitrate_bps, quality.fps, quality.source_start_sec, path)
                            candidate.artifact = path
                            candidate.artifact_hash = file_hash(path)
                        current_signature = runtime.stream_signature(path)
                        if signature is not None and signature != current_signature:
                            raise UnsupportedV2("Shot decoder configurations are incompatible for video-copy assembly.")
                        signature = current_signature
                        paths.append(path)
                        _emit_progress(progress_callback, state="running_pass", percent=70 * (index + 1) / len(selected),
                                       file_progress=70 * (index + 1) / len(selected), current_pass_index=1,
                                       total_passes=1, **context)
                    manifest = runtime.root / "selected.ffconcat"
                    manifest.write_text(concat_manifest(paths, [a.shot.frame_count / quality.fps for a in quality.shots]), encoding="utf-8")
                    video = runtime.root / "assembled.mkv"
                    runtime.run(build_concat_command(ffmpeg, manifest, video), "v2 assembly")
                    runtime.run(build_mux_command(ffmpeg, item, video, temporary, quality.source_start_sec), "v2 final mux")
                    fps, _, origin, count = runtime.timeline(temporary)
                    if (count != quality.source_frames or not math.isclose(fps, quality.fps, rel_tol=1e-8)
                            or abs(origin) > max(0.0011, 0.01 / quality.fps)):
                        raise RuntimeError("Final assembly changed the source frame count or cadence.")
                    _validate_auxiliary(runtime, temporary, quality.source_start_sec)
                    _emit_progress(progress_callback, state="validating", percent=80, file_progress=80, **context)
                    with analysis_slot(cancel_check):
                        scores = runtime.score(temporary, ShotRange(0, quality.source_frames), quality.fps, quality.source_start_sec)
                    failed = validate_scores(quality, scores, item.options.min_vmaf)
                    for index, (candidate, path) in enumerate(zip(selected, paths)):
                        candidate.mean_vmaf = quality.final_shot_means[index]
                        candidate.worst_1s_vmaf = quality.final_shot_worst_1s[index]
                        candidate.predicted_video_bytes = runtime.video_bytes(path)
                        candidate.whole_shot = candidate.full_verified = candidate.holdout_verified = True
                        candidate.measured_frames = quality.shots[index].shot.frame_count
                    quality.refinement_rounds = round_index
                    save(root, quality)
                    uncommitted_assets.clear()
                    if not failed:
                        break
                    if round_index == settings.refinement_limit:
                        quality.status = QualitySearchStatus.CONSTRAINT_UNSATISFIED
                        quality.failure_kind = ConstraintFailureKind.QUALITY_UNREACHABLE
                        quality.reason = "Smart v2 final quality/temporal gate failed after bounded local refinement."
                        result.error_message = quality.reason
                        result.needs_decision = True
                        return result
                    runtime.emit("refining", f"Smart v2: correcting shots {[i + 1 for i in failed]}.")
                    for index in failed:
                        analysis, old = quality.shots[index], selected[index]
                        if any(i in (index, index - 1) and q < max(0, item.options.min_vmaf - 8)
                               for i, q in enumerate(quality.final_boundary_worst_1s)):
                            old.worst_1s_vmaf = min(old.worst_1s_vmaf, min(quality.final_boundary_worst_1s))
                        ceiling = item.options.max_video_kbps * 1000 or max(old.bitrate_bps * 2, item.media_info.video_bitrate_bps if item.media_info else 0)
                        rate = min(ceiling, math.ceil(old.bitrate_bps * 1.5 / 1000) * 1000)
                        if rate > old.bitrate_bps:
                            before = set(assets.iterdir())
                            try:
                                with analysis_slot(cancel_check):
                                    measure_candidate(runtime, quality, analysis, rate, assets)
                            finally:
                                uncommitted_assets.update(set(assets.iterdir()) - before)
                    reselect(quality, item)
                    if not quality.success:
                        result.error_message = quality.reason
                        result.needs_decision = True
                        return result
                assert temporary is not None
                runtime.check()
                actual = temporary.stat().st_size
                if quality.measurement_fingerprint != fingerprint(ffmpeg, ffprobe, item):
                    raise RuntimeError("Source or measurement settings changed during Smart v2 encoding.")
                result.actual_output_bytes, result.allowed_output_bytes = actual, quality.max_output_bytes
                if actual > quality.max_output_bytes:
                    rejected = _size_miss_output_path(item.output_path)
                    temporary.replace(rejected)
                    temporary = None
                    result.rejected_output_path, result.needs_decision = rejected, True
                    result.error_message = f"Smart v2 actual size {actual} exceeds {quality.max_output_bytes}; preserved at {rejected}."
                    _emit_progress(progress_callback, state="needs_decision", message=result.error_message, **context)
                    return result
                if item.output_path.exists() and not item.options.overwrite:
                    raise FileExistsError("Output appeared during Smart v2 encoding and overwrite is disabled.")
                runtime.check()
                os.replace(temporary, item.output_path)
                temporary = None
                result.success = True
                _copy_external_subtitles_for_result(item, result, queue_index, queue_total, log_callback)
                _emit_progress(progress_callback, state="finished_file", percent=100, file_progress=100,
                               segmented_analysis_result=quality, **context)
                return result
            finally:
                result.commands = runtime.commands
                quality.candidate_encodes += runtime.encodes
                quality.vmaf_executions += runtime.vmaf_calls
                for phase, seconds in runtime.times.items():
                    quality.phase_seconds[phase] = quality.phase_seconds.get(phase, 0) + seconds
                for phase, seconds in runtime.cpu_times.items():
                    quality.phase_cpu_seconds[phase] = quality.phase_cpu_seconds.get(phase, 0) + seconds
                quality.resource_cpu_seconds = sum(quality.phase_cpu_seconds.values()) or None
    except OperationCancelledError:
        _emit_progress(progress_callback, state="cancelled_file", **context)
        raise
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, UnsupportedV2) and quality is not None:
            quality.status, quality.reason = QualitySearchStatus.UNSUPPORTED, str(exc)
        result.error_message = str(exc)
        result.return_code = exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1
        _emit(log_callback, result.error_message)
        _emit_progress(progress_callback, state="failed_file", message=result.error_message, **context)
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        for path in uncommitted_assets:
            path.unlink(missing_ok=True)
