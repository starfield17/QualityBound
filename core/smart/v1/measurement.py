"""FFmpeg/VMAF command execution and candidate measurement for Smart analysis."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, TextIO

from core.ffmpeg.commands import build_encode_commands, build_video_args
from core.models import (
    ContainerChoice,
    DecodeAcceleration,
    EncodePlanItem,
    OperationCancelledError,
    QualityCandidateResult,
    VmafBackend,
)
from core.ffmpeg.subprocess import hidden_popen_kwargs

from .runtime import SOURCE_DECODE_SOFTWARE, AnalysisExecutionPlan, AnalysisTier, source_decode_args
from .size_prediction import predict_size_distribution
from .vmaf import (
    EXACT_VMAF_SUBSAMPLE,
    PTS_RESET_FILTER,
    VmafEncodeMetadata,
    VmafModelSpec,
    VmafWindowScore,
    build_cpu_vmaf_command,
    build_cpu_vmaf_filter_graph,
    build_cuda_vmaf_command,
    candidate_encode_metadata,
    parse_vmaf_json,
    select_vmaf_model,
)


SMART_ERROR_TAIL_CHARS = 4_000


class SmartCommandError(subprocess.CalledProcessError):
    """A failed Smart command with its phase and bounded output tail."""

    def __init__(self, returncode: int, cmd: list[str], phase: str, output: str) -> None:
        self.phase = phase
        self.output_tail = output[-SMART_ERROR_TAIL_CHARS:]
        command = " ".join(str(part) for part in cmd)
        tail = self.output_tail.strip() or "(no command output)"
        diagnostic = (
            f"Smart {phase} failed with exit code {returncode}: {command}\n"
            f"Output tail (last {SMART_ERROR_TAIL_CHARS} characters):\n{tail}"
        )
        super().__init__(returncode, cmd, output=diagnostic, stderr=diagnostic)

    def __str__(self) -> str:
        command = " ".join(str(part) for part in self.cmd)
        tail = self.output_tail.strip() or "(no command output)"
        return (
            f"Smart {self.phase} failed with exit code {self.returncode}: {command}\n"
            f"Output tail (last {SMART_ERROR_TAIL_CHARS} characters):\n{tail}"
        )


@dataclass(frozen=True, slots=True)
class SampleWindow:
    start_sec: float
    duration_sec: float


@dataclass(frozen=True, slots=True)
class WindowMeasurement:
    score: VmafWindowScore
    encoded_bytes: int
    duration_sec: float


def _measurement_key(
    ffmpeg_path: Path, item: EncodePlanItem, plan: AnalysisExecutionPlan,
    source: Path, duration: float, start: float = 0.0,
) -> str:
    def identity(path: Path) -> tuple[str, int | None, int | None]:
        try:
            stat = path.stat()
            return str(path.resolve()), stat.st_size, stat.st_mtime_ns
        except OSError:
            return str(path.resolve()), None, None

    assert item.media_info is not None
    payload = (
        identity(ffmpeg_path), identity(item.source_path), identity(source), start, duration,
        build_video_args(item, plan.encoder_extra_args), asdict(plan),
        asdict(select_vmaf_model(item.media_info, item.options.viewing_context)),
        asdict(candidate_encode_metadata(item.media_info, item.options.pix_fmt)),
        item.media_info.fps,
    )
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def run_logged(
    cmd: list[str],
    log_file: TextIO,
    *,
    cancel_check: Callable[[], bool] | None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None,
    cwd: Path | None = None,
    phase: str = "command",
    capture_output: bool = False,
) -> str:
    started = time.perf_counter()
    log_file.write("$ " + " ".join(cmd) + "\n")
    log_file.flush()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=cwd,
            **hidden_popen_kwargs(),
        )
    except OSError as exc:
        os_error = str(exc)[:SMART_ERROR_TAIL_CHARS]
        log_file.write(f"[smart process start failed] phase={phase}\nOS error: {os_error}\n")
        log_file.flush()
        command = " ".join(str(part) for part in cmd)
        raise RuntimeError(
            f"Smart {phase} failed to start (exit code unavailable): {command}\nOutput tail: {os_error}"
        ) from exc
    if process_callback is not None:
        process_callback(proc)
    output_tail = ""
    captured: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            output_tail = (output_tail + line)[-SMART_ERROR_TAIL_CHARS:]
            if capture_output:
                captured.append(line)
            log_file.write(line)
            if cancel_check is not None and cancel_check():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise OperationCancelledError("Smart analysis cancelled.")
        return_code = proc.wait()
    finally:
        if process_callback is not None:
            process_callback(None)
        log_file.write("[smart phase] " + json.dumps({
            "phase": phase, "seconds": time.perf_counter() - started,
        }) + "\n")
        log_file.flush()
    if return_code != 0:
        failure = SmartCommandError(return_code, cmd, phase, output_tail)
        log_file.write(f"[smart command failed] phase={phase} exit_code={return_code}\n{failure.output_tail}\n")
        log_file.flush()
        raise failure
    return "".join(captured)


def log_timing(log_file: TextIO, message: str) -> None:
    log_file.write(f"[smart timing] {message}\n")
    log_file.flush()


def build_reference(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    window: SampleWindow,
    output_path: Path,
    *,
    decode_acceleration: str = SOURCE_DECODE_SOFTWARE,
) -> list[str]:
    return [
        str(ffmpeg_path), "-hide_banner", "-y", *source_decode_args(decode_acceleration),
        "-ss", f"{window.start_sec:.3f}", "-t", f"{window.duration_sec:.3f}",
        "-i", str(item.source_path), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", PTS_RESET_FILTER,
        "-c:v", "ffv1", "-level", "3", "-g", "1", str(output_path),
    ]


def loopback_decoder_name(encoder_name: str) -> str:
    lowered = encoder_name.lower()
    if "av1" in lowered:
        return "av1"
    if "hevc" in lowered or "x265" in lowered:
        return "hevc"
    if "h264" in lowered or "x264" in lowered:
        return "h264"
    return "hevc"


def build_loopback_score_command(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    window: SampleWindow,
    output_path: Path,
    plan: AnalysisExecutionPlan,
    *,
    model_spec: VmafModelSpec,
    encode_metadata: VmafEncodeMetadata,
    log_name: str,
) -> list[str]:
    if plan.vmaf_backend != VmafBackend.CPU:
        raise ValueError("VMAF v1 loopback scoring currently supports only the CPU backend.")
    video_args = build_video_args(item, extra_args=plan.encoder_extra_args)
    filter_graph = build_cpu_vmaf_filter_graph(
        model_spec=model_spec, encode_metadata=encode_metadata, log_path=log_name,
        n_threads=plan.vmaf_threads, n_subsample=plan.vmaf_subsample,
        distorted_input="dec:v", reference_input="0:v",
    )
    return [
        str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
        *source_decode_args(plan.source_decode_acceleration), "-ss", f"{window.start_sec:.3f}",
        "-t", f"{window.duration_sec:.3f}", "-i", str(item.source_path),
        "-map", "0:v:0", "-an", "-sn", "-dn", *video_args, "-dec:v",
        loopback_decoder_name(plan.encoder_name), str(output_path), "-filter_complex", filter_graph,
        "-an", "-f", "null", "-",
    ]


def bind_candidate_item(item: EncodePlanItem, bitrate_bps: int, plan: AnalysisExecutionPlan) -> EncodePlanItem:
    encoder_info = item.encoder_info
    if encoder_info is not None and encoder_info.encoder_name != plan.encoder_name:
        encoder_info = replace(encoder_info, encoder_name=plan.encoder_name)
    return replace(
        item,
        encoder_info=encoder_info,
        options=replace(
            item.options, decode_acceleration=DecodeAcceleration.SOFTWARE, container=ContainerChoice.MKV,
            copy_subtitles=False, copy_external_subtitles=False, overwrite=True,
            encoder_preset=plan.encoder_preset, two_pass=plan.two_pass,
        ),
        target_video_bitrate_bps=bitrate_bps,
    )


def vmaf_command(
    ffmpeg_path: Path, plan: AnalysisExecutionPlan, *, candidate_path: Path, reference_path: Path,
    model_spec: VmafModelSpec, encode_metadata: VmafEncodeMetadata, log_name: str,
) -> list[str]:
    if plan.vmaf_backend == VmafBackend.CUDA:
        return build_cuda_vmaf_command(
            ffmpeg_path, distorted_path=candidate_path, reference_path=reference_path,
            model_spec=model_spec, encode_metadata=encode_metadata, log_name=log_name,
            n_threads=plan.vmaf_threads, n_subsample=plan.vmaf_subsample,
        )
    return build_cpu_vmaf_command(
        ffmpeg_path, distorted_path=candidate_path, reference_path=reference_path,
        model_spec=model_spec, encode_metadata=encode_metadata, log_name=log_name,
        n_threads=plan.vmaf_threads, n_subsample=plan.vmaf_subsample,
    )


def candidate_result(
    item: EncodePlanItem, bitrate_bps: int, scores: list[VmafWindowScore], encoded_bytes: list[int],
    encoded_durations_sec: list[float], audio_bitrate_bps: int, source_bytes: int | None,
) -> QualityCandidateResult:
    observed_window_bitrates = [
        int(math.ceil(encoded_size * 8.0 / duration))
        for encoded_size, duration in zip(encoded_bytes, encoded_durations_sec)
    ]
    prediction = predict_size_distribution(
        requested_bitrate_bps=bitrate_bps,
        observed_window_bitrates=observed_window_bitrates,
        duration_sec=item.media_info.duration if item.media_info is not None else 0.0,
        audio_bitrate_bps=audio_bitrate_bps,
        source_bytes=source_bytes,
    )
    return QualityCandidateResult(
        video_bitrate_bps=bitrate_bps,
        segment_vmaf=[score.gate_score for score in scores],
        min_vmaf=min(score.gate_score for score in scores),
        encoded_bytes=encoded_bytes, encoded_durations_sec=encoded_durations_sec,
        observed_window_bitrates=observed_window_bitrates,
        observed_video_bitrate_bps=prediction.mean_video_bitrate_bps,
        predicted_output_bytes=prediction.predicted_output_bytes,
        predicted_output_ratio=prediction.predicted_output_ratio,
        size_prediction=prediction,
        segment_mean_vmaf=[score.mean for score in scores],
        segment_p10_vmaf=[score.p10 for score in scores],
        segment_worst_1s_vmaf=[score.worst_1s for score in scores],
        segment_quality_scores=[score.gate_score for score in scores],
    )


def measure_size_only(
    ffmpeg_path: Path,
    item: EncodePlanItem,
    windows: list[SampleWindow],
    bitrate_bps: int,
    temp_root: Path,
    log_file: TextIO,
    plan: AnalysisExecutionPlan,
    *,
    cancel_check: Callable[[], bool] | None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None,
) -> list[int]:
    """Encode representative windows without VMAF for borderline size calibration."""

    candidate_item = bind_candidate_item(item, bitrate_bps, plan)
    video_args = build_video_args(candidate_item, extra_args=plan.encoder_extra_args)
    measured: list[int] = []
    for index, window in enumerate(windows):
        output_path = temp_root / f"size-calibration-{bitrate_bps}-{index}.mkv"
        command = [
            str(ffmpeg_path), "-hide_banner", "-loglevel", "error", "-y",
            *source_decode_args(plan.source_decode_acceleration),
            "-ss", f"{window.start_sec:.3f}", "-t", f"{window.duration_sec:.3f}",
            "-i", str(item.source_path), "-map", "0:v:0", "-an", "-sn", "-dn",
            *video_args, str(output_path),
        ]
        run_logged(
            command,
            log_file,
            cancel_check=cancel_check,
            process_callback=process_callback,
            phase="representative size calibration",
        )
        encoded_size = output_path.stat().st_size
        measured.append(int(math.ceil(encoded_size * 8.0 / window.duration_sec)))
    return measured


def score_candidate_loopback(
    ffmpeg_path: Path, item: EncodePlanItem, windows: list[SampleWindow], bitrate_bps: int,
    temp_root: Path, log_file: TextIO, plan: AnalysisExecutionPlan, *,
    cancel_check: Callable[[], bool] | None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None,
    audio_bitrate_bps: int = 0, source_bytes: int | None = None,
    min_vmaf_target: float | None = None, window_order: list[int] | None = None,
    measurement_cache: dict[str, WindowMeasurement] | None = None,
    force_remeasure: bool = False,
) -> QualityCandidateResult:
    candidate_item = bind_candidate_item(item, bitrate_bps, plan)
    if item.media_info is None:
        raise ValueError("Smart analysis requires probed media.")
    model_spec = select_vmaf_model(item.media_info, item.options.viewing_context)
    encode_metadata = candidate_encode_metadata(item.media_info, item.options.pix_fmt)
    order = window_order or list(range(len(windows)))
    scores: dict[int, VmafWindowScore] = {}
    encoded_bytes: dict[int, int] = {}
    encoded_durations: dict[int, float] = {}
    for window_index in order:
        if cancel_check is not None and cancel_check():
            raise OperationCancelledError("Smart analysis cancelled.")
        window = windows[window_index]
        cache_key = _measurement_key(ffmpeg_path, candidate_item, plan, item.source_path,
                                     window.duration_sec, window.start_sec)
        cached = measurement_cache.get(cache_key) if measurement_cache is not None and not force_remeasure else None
        if cached is not None:
            scores[window_index] = cached.score
            encoded_bytes[window_index] = cached.encoded_bytes
            encoded_durations[window_index] = cached.duration_sec
            log_timing(log_file, f"measurement cache hit: {cache_key}")
            if min_vmaf_target is not None and cached.score.gate_score < min_vmaf_target:
                break
            continue
        candidate_path = temp_root / f"loopback-{plan.tier.value}-{bitrate_bps}-{window_index}.mkv"
        json_path = temp_root / f"vmaf-{plan.tier.value}-{bitrate_bps}-{window_index}.json"
        started = time.perf_counter()
        run_logged(
            build_loopback_score_command(
                ffmpeg_path, candidate_item, window, candidate_path, plan,
                model_spec=model_spec, encode_metadata=encode_metadata, log_name=json_path.name,
            ), log_file, cancel_check=cancel_check, process_callback=process_callback,
            cwd=temp_root, phase="loopback score",
        )
        elapsed = time.perf_counter() - started
        log_timing(log_file, f"{plan.tier.value} candidate {bitrate_bps} window {window_index + 1} loopback={elapsed:.2f}s")
        try:
            encoded_size = candidate_path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"Smart loopback encode did not produce its sample output: {candidate_path}") from exc
        if window.duration_sec <= 0:
            raise RuntimeError(f"Smart candidate encode produced an invalid sample duration: {window.duration_sec}")
        score = parse_vmaf_json(json_path, model_spec, fps=item.media_info.fps)
        scores[window_index] = score
        encoded_bytes[window_index] = encoded_size
        encoded_durations[window_index] = float(window.duration_sec)
        if cancel_check is not None and cancel_check():
            raise OperationCancelledError("Smart analysis cancelled.")
        if measurement_cache is not None:
            measurement_cache[cache_key] = WindowMeasurement(score, encoded_size, window.duration_sec)
        if min_vmaf_target is not None and score.gate_score < min_vmaf_target:
            log_timing(log_file, f"{plan.tier.value} candidate {bitrate_bps}: window {window_index + 1} quality_score={score.gate_score:.3f} early rejected")
            break
    measured = sorted(scores)
    return candidate_result(
        item, bitrate_bps, [scores[index] for index in measured],
        [encoded_bytes[index] for index in measured], [encoded_durations[index] for index in measured],
        audio_bitrate_bps, source_bytes,
    )


def score_candidate(
    ffmpeg_path: Path, item: EncodePlanItem, references: list[Path], bitrate_bps: int,
    temp_root: Path, workdir: Path, log_file: TextIO, *,
    cancel_check: Callable[[], bool] | None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None,
    window_durations_sec: list[float] | None = None, audio_bitrate_bps: int = 0,
    source_bytes: int | None = None, plan: AnalysisExecutionPlan | None = None,
    min_vmaf_target: float | None = None, window_order: list[int] | None = None,
    measurement_cache: dict[str, WindowMeasurement] | None = None,
    force_remeasure: bool = False,
) -> QualityCandidateResult:
    if plan is None:
        if item.encoder_info is None:
            raise ValueError("Smart analysis requires a bound encoder.")
        plan = AnalysisExecutionPlan(
            tier=AnalysisTier.EXACT, source_decode_acceleration=SOURCE_DECODE_SOFTWARE,
            encoder_name=item.encoder_info.encoder_name, encoder_preset=item.options.encoder_preset,
            encoder_extra_args=(), two_pass=bool(item.options.two_pass and item.encoder_info.supports_two_pass),
            vmaf_backend=VmafBackend.CPU, vmaf_threads=1, vmaf_subsample=EXACT_VMAF_SUBSAMPLE,
            use_loopback=False,
        )
    scores: dict[int, VmafWindowScore] = {}
    encoded_bytes: dict[int, int] = {}
    encoded_durations: dict[int, float] = {}
    candidate_item = bind_candidate_item(item, bitrate_bps, plan)
    if item.media_info is None:
        raise ValueError("Smart analysis requires probed media.")
    model_spec = select_vmaf_model(item.media_info, item.options.viewing_context)
    encode_metadata = candidate_encode_metadata(item.media_info, item.options.pix_fmt)
    order = window_order or list(range(len(references)))
    for window_index in order:
        if cancel_check is not None and cancel_check():
            raise OperationCancelledError("Smart analysis cancelled.")
        reference = references[window_index]
        duration = (
            window_durations_sec[window_index]
            if window_durations_sec is not None and window_index < len(window_durations_sec)
            else item.media_info.duration
        )
        if duration <= 0:
            raise RuntimeError(f"Smart candidate encode produced an invalid sample duration: {duration}")
        cache_key = _measurement_key(ffmpeg_path, candidate_item, plan, reference, duration)
        cached = measurement_cache.get(cache_key) if measurement_cache is not None and not force_remeasure else None
        if cached is not None:
            scores[window_index] = cached.score
            encoded_bytes[window_index] = cached.encoded_bytes
            encoded_durations[window_index] = cached.duration_sec
            log_timing(log_file, f"measurement cache hit: {cache_key}")
            if min_vmaf_target is not None and cached.score.gate_score < min_vmaf_target:
                break
            continue
        candidate_path = temp_root / f"candidate-{plan.tier.value}-{bitrate_bps}-{window_index}.mkv"
        commands, passlog = build_encode_commands(
            ffmpeg_path, candidate_item, workdir, input_path=reference, output_path=candidate_path,
            stage=f"smart-{plan.tier.value}-{bitrate_bps}-{window_index}", extra_video_args=plan.encoder_extra_args,
        )
        encode_started = time.perf_counter()
        try:
            for command in commands:
                run_logged(command, log_file, cancel_check=cancel_check, process_callback=process_callback, phase="candidate encode")
        finally:
            if passlog:
                for leftover in passlog.parent.glob(passlog.name + "*"):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
        encode_elapsed = time.perf_counter() - encode_started
        try:
            encoded_size = candidate_path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"Smart candidate encode did not produce its sample output: {candidate_path}") from exc
        json_path = temp_root / f"vmaf-{plan.tier.value}-{bitrate_bps}-{window_index}.json"
        vmaf_started = time.perf_counter()
        run_logged(
            vmaf_command(
                ffmpeg_path, plan, candidate_path=candidate_path, reference_path=reference,
                model_spec=model_spec, encode_metadata=encode_metadata, log_name=json_path.name,
            ), log_file, cancel_check=cancel_check, process_callback=process_callback,
            cwd=temp_root, phase="VMAF scoring",
        )
        vmaf_elapsed = time.perf_counter() - vmaf_started
        score = parse_vmaf_json(json_path, model_spec, fps=item.media_info.fps)
        scores[window_index] = score
        encoded_bytes[window_index] = encoded_size
        encoded_durations[window_index] = float(duration)
        if cancel_check is not None and cancel_check():
            raise OperationCancelledError("Smart analysis cancelled.")
        if measurement_cache is not None:
            measurement_cache[cache_key] = WindowMeasurement(score, encoded_size, duration)
        log_timing(log_file, f"{plan.tier.value} candidate {bitrate_bps} window {window_index + 1}: encode={encode_elapsed:.2f}s vmaf={vmaf_elapsed:.2f}s quality_score={score.gate_score:.3f}")
        if min_vmaf_target is not None and score.gate_score < min_vmaf_target:
            log_timing(log_file, f"{plan.tier.value} candidate {bitrate_bps}: window {window_index + 1} quality_score={score.gate_score:.3f} early rejected")
            break
    measured = sorted(scores)
    return candidate_result(
        item, bitrate_bps, [scores[index] for index in measured],
        [encoded_bytes[index] for index in measured], [encoded_durations[index] for index in measured],
        audio_bitrate_bps, source_bytes,
    )
