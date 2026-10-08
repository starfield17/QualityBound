from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.ffmpeg.subprocess import noninteractive_run_kwargs
from core.media.metadata import infer_bit_depth_from_pix_fmt
from core.models import MediaInfo, VmafBackend, VmafRuntimeSupport, VmafViewingContext


PTS_RESET_FILTER = "settb=AVTB,setpts=PTS-STARTPTS"
COARSE_VMAF_SUBSAMPLE = 3
EXACT_VMAF_SUBSAMPLE = 1
MAX_VMAF_THREADS = 8

VMAF_MODEL_GENERATION = "v1"
VMAF_MODEL_VERSION = "1.0.16"
VMAF_MEASUREMENT_PIX_FMT = "yuv420p10le"
VMAF_MEASUREMENT_BIT_DEPTH = 10
VMAF_SCALE_FLAGS = "bicubic"
VMAF_ASPECT_POLICY = "square_pixels_fit_and_even_pad"
VMAF_RESOLUTION_MODE = "display_model_canvas"
VMAF_HFR_MIN_FPS = 50.0
VMAF_MEASUREMENT_PIPELINE_VERSION = 3
TEMPORAL_DROP_TOLERANCE = 4.0
VMAF_PROBE_STANDARD_FPS = 30
VMAF_PROBE_STANDARD_FRAMES = 6
VMAF_PROBE_HFR_FPS = 60
VMAF_PROBE_HFR_FRAMES = 12


@dataclass(frozen=True, slots=True)
class VmafModelSpec:
    name: str
    generation: str
    display_width: int
    display_height: int
    hfr: bool
    score_min: float = 0.0
    score_max: float = 100.0


@dataclass(frozen=True, slots=True)
class VmafEncodeMetadata:
    width: int | None
    height: int | None
    bit_depth: int | None


@dataclass(frozen=True, slots=True)
class VmafWindowScore:
    mean: float
    p10: float
    worst_1s: float
    gate_score: float


VMAF_STANDARD_MODEL = VmafModelSpec(
    "vmaf_v1.0.16_3d0h", VMAF_MODEL_GENERATION, 1920, 1080, False
)
VMAF_STANDARD_HFR_MODEL = VmafModelSpec(
    "vmaf_v1.0.16_hfr_3d0h", VMAF_MODEL_GENERATION, 1920, 1080, True
)
VMAF_4K_MODEL = VmafModelSpec(
    "vmaf_v1.0.16_1d5h_2160", VMAF_MODEL_GENERATION, 3840, 2160, False
)
VMAF_4K_HFR_MODEL = VmafModelSpec(
    "vmaf_v1.0.16_hfr_1d5h_2160", VMAF_MODEL_GENERATION, 3840, 2160, True
)
VMAF_PRODUCTION_MODELS = (
    VMAF_STANDARD_MODEL,
    VMAF_4K_MODEL,
    VMAF_STANDARD_HFR_MODEL,
    VMAF_4K_HFR_MODEL,
)

# Future GPU backends belong here only after their exact v1 smoke probe passes.
VMAF_BACKEND_POLICY = (VmafBackend.CPU,)


class InvalidVmafSubsample(ValueError):
    pass


def is_4k_geometry(width: int | None, height: int | None) -> bool:
    if width is None or height is None:
        return False
    short_side, long_side = sorted((width, height))
    return short_side >= 2160 and long_side >= 3840


def select_vmaf_model(
    media_info: MediaInfo,
    viewing_context: VmafViewingContext = VmafViewingContext.HIGH_FIDELITY,
) -> VmafModelSpec:
    context = VmafViewingContext(viewing_context)
    is_4k = (
        context == VmafViewingContext.HIGH_FIDELITY
        and is_4k_geometry(media_info.width, media_info.height)
    )
    is_hfr = media_info.fps is not None and media_info.fps >= VMAF_HFR_MIN_FPS
    if is_4k:
        return VMAF_4K_HFR_MODEL if is_hfr else VMAF_4K_MODEL
    return VMAF_STANDARD_HFR_MODEL if is_hfr else VMAF_STANDARD_MODEL


_VMAF_SCORE_RE = re.compile(
    r"VMAF score:\s*([+-]?(?:nan|inf(?:inity)?|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?))",
    re.IGNORECASE,
)


def candidate_encode_metadata(media_info: MediaInfo, pix_fmt: str | None) -> VmafEncodeMetadata:
    return VmafEncodeMetadata(
        width=media_info.width,
        height=media_info.height,
        bit_depth=infer_bit_depth_from_pix_fmt(pix_fmt),
    )


def validate_vmaf_subsample(n_subsample: int) -> int:
    """Reject even subsample values; Netflix warns they can be inaccurate."""
    value = int(n_subsample)
    if value < 1:
        raise InvalidVmafSubsample("VMAF n_subsample must be at least 1.")
    if value % 2 == 0:
        raise InvalidVmafSubsample(
            f"VMAF n_subsample={value} is even; even values can produce inaccurate scores."
        )
    return value


def vmaf_thread_budget(active_cpu_vmaf_jobs: int = 1) -> int:
    cpu_count = os.cpu_count() or 4
    reserve = 1 if cpu_count <= 4 else 2
    available = max(1, cpu_count - reserve)
    jobs = max(1, int(active_cpu_vmaf_jobs))
    return max(1, min(MAX_VMAF_THREADS, available // jobs))


def build_vmaf_model_config(
    model_spec: VmafModelSpec,
    encode_metadata: VmafEncodeMetadata,
) -> str:
    parts = [f"version={model_spec.name}"]
    if encode_metadata.width is not None:
        parts.append(f"cambi.enc_width={int(encode_metadata.width)}")
    if encode_metadata.height is not None:
        parts.append(f"cambi.enc_height={int(encode_metadata.height)}")
    if encode_metadata.bit_depth is not None:
        parts.append(f"cambi.enc_bitdepth={int(encode_metadata.bit_depth)}")
    return ":".join(parts)


def quote_libvmaf_model_config(model_config: str) -> str:
    """Quote a nested libvmaf model dictionary for an FFmpeg filter graph."""
    escaped = model_config.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"'{escaped}'"


def build_libvmaf_option(
    *,
    model_spec: VmafModelSpec,
    encode_metadata: VmafEncodeMetadata,
    log_path: str | None,
    n_threads: int,
    n_subsample: int,
    filter_name: str = "libvmaf",
) -> str:
    subsample = validate_vmaf_subsample(n_subsample)
    threads = max(1, int(n_threads))
    model_config = quote_libvmaf_model_config(
        build_vmaf_model_config(model_spec, encode_metadata)
    )
    options = [
        f"model={model_config}",
        f"n_threads={threads}",
        f"n_subsample={subsample}",
    ]
    if log_path is not None:
        options.extend(("log_fmt=json", f"log_path='{log_path}'"))
    return f"{filter_name}=" + ":".join(options)


def display_normalization_filter(model_spec: VmafModelSpec) -> str:
    width = model_spec.display_width
    height = model_spec.display_height
    return (
        "scale=w='max(2,trunc(iw*sar/2)*2)':"
        f"h='max(2,trunc(ih/2)*2)':flags={VMAF_SCALE_FLAGS},setsar=1,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2:flags={VMAF_SCALE_FLAGS},"
        f"pad={width}:{height}:x='trunc((ow-iw)/4)*2':"
        "y='trunc((oh-ih)/4)*2',"
        f"setsar=1,format={VMAF_MEASUREMENT_PIX_FMT},{PTS_RESET_FILTER}"
    )


def build_cpu_vmaf_filter_graph(
    *,
    model_spec: VmafModelSpec,
    encode_metadata: VmafEncodeMetadata,
    log_path: str | None,
    n_threads: int,
    n_subsample: int,
    distorted_input: str = "0:v",
    reference_input: str = "1:v",
) -> str:
    normalize = display_normalization_filter(model_spec)
    libvmaf = build_libvmaf_option(
        model_spec=model_spec,
        encode_metadata=encode_metadata,
        log_path=log_path,
        n_threads=n_threads,
        n_subsample=n_subsample,
    )
    return (
        f"[{distorted_input}]{normalize}[dist];"
        f"[{reference_input}]{normalize}[ref];"
        f"[dist][ref]{libvmaf}"
    )


def build_cpu_vmaf_command(
    ffmpeg_path: Path,
    *,
    distorted_path: Path,
    reference_path: Path,
    model_spec: VmafModelSpec,
    encode_metadata: VmafEncodeMetadata,
    log_name: str,
    n_threads: int,
    n_subsample: int,
) -> list[str]:
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(distorted_path),
        "-i",
        str(reference_path),
        "-filter_complex",
        build_cpu_vmaf_filter_graph(
            model_spec=model_spec,
            encode_metadata=encode_metadata,
            log_path=log_name,
            n_threads=n_threads,
            n_subsample=n_subsample,
        ),
        "-an",
        "-f",
        "null",
        "-",
    ]


def build_cuda_vmaf_command(
    ffmpeg_path: Path,
    *,
    distorted_path: Path,
    reference_path: Path,
    model_spec: VmafModelSpec,
    encode_metadata: VmafEncodeMetadata,
    log_name: str,
    n_threads: int,
    n_subsample: int,
) -> list[str]:
    """Legacy/future CUDA infrastructure; v1 production policy does not call it."""
    if model_spec.generation == VMAF_MODEL_GENERATION:
        raise ValueError(
            "VMAF v1 CUDA measurement is not implemented; use the CPU runtime probe."
        )
    libvmaf = build_libvmaf_option(
        model_spec=model_spec,
        encode_metadata=encode_metadata,
        log_path=log_name,
        n_threads=n_threads,
        n_subsample=n_subsample,
        filter_name="libvmaf_cuda",
    )
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-hwaccel",
        "cuda",
        "-hwaccel_output_format",
        "cuda",
        "-i",
        str(distorted_path),
        "-hwaccel",
        "cuda",
        "-hwaccel_output_format",
        "cuda",
        "-i",
        str(reference_path),
        "-filter_complex",
        f"[0:v]scale_cuda=format=yuv420p,{PTS_RESET_FILTER}[dist];"
        f"[1:v]scale_cuda=format=yuv420p,{PTS_RESET_FILTER}[ref];"
        f"[dist][ref]{libvmaf}",
        "-an",
        "-f",
        "null",
        "-",
    ]


def build_vmaf_probe_command(
    ffmpeg_path: Path,
    model_spec: VmafModelSpec,
    backend: VmafBackend,
) -> list[str]:
    metadata = VmafEncodeMetadata(width=320, height=180, bit_depth=8)
    if backend != VmafBackend.CPU:
        raise ValueError(f"No VMAF v1 runtime probe is implemented for backend {backend.value!r}.")
    fps = VMAF_PROBE_HFR_FPS if model_spec.hfr else VMAF_PROBE_STANDARD_FPS
    frames = VMAF_PROBE_HFR_FRAMES if model_spec.hfr else VMAF_PROBE_STANDARD_FRAMES
    return [
        str(ffmpeg_path),
        "-hide_banner",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate={fps}:duration=1",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate={fps}:duration=1",
        "-filter_complex",
        build_cpu_vmaf_filter_graph(
            model_spec=model_spec,
            encode_metadata=metadata,
            log_path=None,
            n_threads=2,
            n_subsample=1,
        ),
        "-frames:v",
        str(frames),
        "-an",
        "-f",
        "null",
        "-",
    ]


def validate_vmaf_score(score: float, model_spec: VmafModelSpec) -> float:
    if not math.isfinite(score) or not model_spec.score_min <= score <= model_spec.score_max:
        raise RuntimeError(
            f"VMAF model {model_spec.name} produced invalid score {score!r}; "
            f"expected {model_spec.score_min:g}..{model_spec.score_max:g}."
        )
    return score


def parse_vmaf_score(output: str, model_spec: VmafModelSpec) -> float:
    matches = _VMAF_SCORE_RE.findall(output)
    if not matches:
        raise RuntimeError(f"VMAF model {model_spec.name} did not produce a score.")
    return validate_vmaf_score(float(matches[-1]), model_spec)


def _percentile_10(scores: list[float]) -> float:
    ordered = sorted(scores)
    index = max(0, math.ceil(len(ordered) * 0.10) - 1)
    return ordered[index]


def _frame_scores(data: object, path: Path, model_spec: VmafModelSpec) -> tuple[list[float], list[int]]:
    if not isinstance(data, dict) or not isinstance(data.get("frames"), list):
        raise RuntimeError(f"VMAF JSON did not contain per-frame scores: {path}")
    scores: list[float] = []
    frame_numbers: list[int] = []
    for index, frame in enumerate(data["frames"]):
        try:
            if not isinstance(frame, dict):
                raise TypeError
            metrics = frame["metrics"]
            if not isinstance(metrics, dict):
                raise TypeError
            score = validate_vmaf_score(float(metrics["vmaf"]), model_spec)
            frame_number = int(frame.get("frameNum", index))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"VMAF JSON contained an invalid per-frame score: {path}") from exc
        if frame_numbers and frame_number <= frame_numbers[-1]:
            raise RuntimeError(f"VMAF JSON frame numbers were not strictly increasing: {path}")
        scores.append(score)
        frame_numbers.append(frame_number)
    if not scores:
        raise RuntimeError(f"VMAF JSON did not contain per-frame scores: {path}")
    return scores, frame_numbers


def _worst_one_second(
    scores: list[float], frame_numbers: list[int], fps: float | None, model_spec: VmafModelSpec
) -> float:
    effective_fps = float(fps) if fps is not None and math.isfinite(fps) and fps > 0 else (
        60.0 if model_spec.hfr else 30.0
    )
    deltas = [right - left for left, right in zip(frame_numbers, frame_numbers[1:])]
    cadence = sorted(deltas)[len(deltas) // 2] if deltas else 1
    window_size = min(len(scores), max(1, int(math.ceil(effective_fps / max(1, cadence)))))
    return min(
        sum(scores[start : start + window_size]) / window_size
        for start in range(len(scores) - window_size + 1)
    )


def parse_vmaf_json(
    path: Path,
    model_spec: VmafModelSpec,
    *,
    fps: float | None = None,
) -> VmafWindowScore:
    data = json.loads(path.read_text(encoding="utf-8"))
    try:
        mean = validate_vmaf_score(float(data["pooled_metrics"]["vmaf"]["mean"]), model_spec)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"VMAF JSON did not contain a pooled mean: {path}") from exc
    scores, frame_numbers = _frame_scores(data, path, model_spec)
    p10 = _percentile_10(scores)
    worst_1s = _worst_one_second(scores, frame_numbers, fps, model_spec)
    gate_score = min(mean, worst_1s + TEMPORAL_DROP_TOLERANCE)
    return VmafWindowScore(mean=mean, p10=p10, worst_1s=worst_1s, gate_score=gate_score)


def _run_capture(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **noninteractive_run_kwargs(),
    )


@lru_cache(maxsize=32)
def _probe_vmaf_runtime_cached(
    ffmpeg_path: str,
    size: int | None,
    mtime_ns: int | None,
    model_spec: VmafModelSpec,
    backend: VmafBackend,
    pipeline_version: int,
) -> VmafRuntimeSupport:
    del size, mtime_ns, pipeline_version
    try:
        command = build_vmaf_probe_command(Path(ffmpeg_path), model_spec, backend)
        proc = _run_capture(command)
    except (OSError, ValueError) as exc:
        return VmafRuntimeSupport(backend, model_spec.name, False, str(exc))
    output = proc.stdout + "\n" + proc.stderr
    error = None
    if proc.returncode != 0:
        runnable = False
        detail = output.strip()
        error = detail or f"VMAF {model_spec.name} {backend.value} smoke probe failed."
    else:
        try:
            parse_vmaf_score(output, model_spec)
            runnable = True
        except RuntimeError as exc:
            runnable = False
            error = str(exc)
    return VmafRuntimeSupport(backend, model_spec.name, runnable, error)


def probe_vmaf_runtime(
    ffmpeg_path: Path,
    model_spec: VmafModelSpec,
    backend: VmafBackend,
) -> VmafRuntimeSupport:
    try:
        resolved = ffmpeg_path.resolve()
        stat = resolved.stat()
        size, mtime_ns = stat.st_size, stat.st_mtime_ns
    except OSError:
        resolved = ffmpeg_path
        size, mtime_ns = None, None
    return _probe_vmaf_runtime_cached(
        str(resolved), size, mtime_ns, model_spec, backend, VMAF_MEASUREMENT_PIPELINE_VERSION
    )


def select_vmaf_runtime(
    ffmpeg_path: Path,
    model_spec: VmafModelSpec,
    backend_policy: tuple[VmafBackend, ...] = VMAF_BACKEND_POLICY,
) -> VmafRuntimeSupport:
    failures: list[str] = []
    for backend in backend_policy:
        support = probe_vmaf_runtime(ffmpeg_path, model_spec, backend)
        if support.runnable:
            return support
        if support.error_message:
            failures.append(f"{backend.value}: {support.error_message}")
    return VmafRuntimeSupport(
        backend=backend_policy[-1] if backend_policy else VmafBackend.CPU,
        model=model_spec.name,
        runnable=False,
        error_message="; ".join(failures) or f"No backend can run VMAF model {model_spec.name}.",
    )
