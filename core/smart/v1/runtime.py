from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from core.ffmpeg.encoders import list_available_encoders, list_available_hwaccels
from core.models import BackendChoice, EncoderInfo, VmafBackend
from core.ffmpeg.subprocess import noninteractive_run_kwargs

from .vmaf import (
    COARSE_VMAF_SUBSAMPLE,
    EXACT_VMAF_SUBSAMPLE,
    vmaf_thread_budget,
)


class AnalysisTier(str, Enum):
    COARSE = "coarse"
    EXACT = "exact"


class AnalysisDecodePolicy(str, Enum):
    AUTO = "auto"
    SOFTWARE = "software"


SOURCE_DECODE_SOFTWARE = "software"
SOURCE_DECODE_VIDEOTOOLBOX = "videotoolbox"
SOURCE_DECODE_CUDA = "cuda"
SOURCE_DECODE_QSV = "qsv"
SOURCE_DECODE_D3D11VA = "d3d11va"
SOURCE_DECODE_VAAPI = "vaapi"

COARSE_MAX_CANDIDATES = 4
EXACT_MAX_CANDIDATES = 3
MIN_SEARCH_TOLERANCE_BPS = 50_000
SEARCH_TOLERANCE_RATIO = 0.03
FILTER_NAME_RE = re.compile(r"^\s*[TSC.]{2,4}\s+(\S+)")
DECODER_NAME_RE = re.compile(r"^\s*[A-Z\.]{6}\s+([^\s]+)")
LOOPBACK_OPTION_RE = re.compile(r"^\s*-dec(?:[:\s\[]|$)")


@dataclass(frozen=True, slots=True)
class AnalysisCapabilities:
    libvmaf: bool
    libvmaf_cuda: bool
    loopback_decoder: bool
    hwaccels: frozenset[str]
    filters: frozenset[str]
    encoders: frozenset[str]
    scale_vt: bool
    scale_cuda: bool
    videotoolbox_hwaccel: bool
    cuda_hwaccel: bool
    videotoolbox_prio_speed: bool


@dataclass(frozen=True, slots=True)
class AnalysisExecutionPlan:
    tier: AnalysisTier
    source_decode_acceleration: str
    encoder_name: str
    encoder_preset: str | None
    encoder_extra_args: tuple[str, ...]
    two_pass: bool
    vmaf_backend: VmafBackend
    vmaf_threads: int
    vmaf_subsample: int
    use_loopback: bool
    fallback_reason: str | None = None

    @property
    def analysis_backend(self) -> str:
        if self.encoder_name.endswith("_videotoolbox"):
            return "videotoolbox"
        if self.encoder_name.endswith("_nvenc"):
            return "nvenc"
        if self.encoder_name.endswith("_qsv"):
            return "qsv"
        if self.encoder_name.endswith("_amf"):
            return "amf"
        return "cpu"


def search_tolerance_bps(
    search_ceiling_bps: int,
    *,
    min_bps: int = MIN_SEARCH_TOLERANCE_BPS,
    ratio: float = SEARCH_TOLERANCE_RATIO,
) -> int:
    return max(int(min_bps), round(int(search_ceiling_bps) * float(ratio)))


def parse_filter_names(output: str) -> frozenset[str]:
    names: set[str] = set()
    for line in output.splitlines():
        match = FILTER_NAME_RE.match(line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


def parse_decoder_names(output: str) -> frozenset[str]:
    names: set[str] = set()
    for line in output.splitlines():
        match = DECODER_NAME_RE.match(line)
        if match:
            names.add(match.group(1))
    return frozenset(names)


def help_exposes_loopback_decoder(help_text: str) -> bool:
    return any(LOOPBACK_OPTION_RE.search(line) for line in help_text.splitlines())


def _run_ffmpeg_list(ffmpeg_path: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            [str(ffmpeg_path), "-hide_banner", *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **noninteractive_run_kwargs(),
        )
    except OSError:
        return ""
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def encoder_help_has_option(help_text: str, option: str) -> bool:
    needle = option if option.startswith("-") else f"-{option}"
    pattern = re.compile(rf"^\s*{re.escape(needle)}(?:\s|$)")
    return any(pattern.search(line) for line in help_text.splitlines())


def _detect_loopback_decoder(ffmpeg_path: Path, decoders: frozenset[str]) -> bool:
    if "loopback" in decoders:
        return True
    help_text = _run_ffmpeg_list(ffmpeg_path, "-h")
    return help_exposes_loopback_decoder(help_text)


@lru_cache(maxsize=8)
def _detect_analysis_capabilities_cached(
    ffmpeg_path: str,
    size: int | None,
    mtime_ns: int | None,
) -> AnalysisCapabilities:
    del size, mtime_ns
    path = Path(ffmpeg_path)
    filters = parse_filter_names(_run_ffmpeg_list(path, "-filters"))
    decoders = parse_decoder_names(_run_ffmpeg_list(path, "-decoders"))
    try:
        hwaccels = frozenset(list_available_hwaccels(path))
    except OSError:
        hwaccels = frozenset()
    try:
        encoders = frozenset(list_available_encoders(path))
    except (OSError, subprocess.CalledProcessError):
        encoders = frozenset()
    vt_help = _run_ffmpeg_list(path, "-h", "encoder=hevc_videotoolbox") if "hevc_videotoolbox" in encoders else ""
    return AnalysisCapabilities(
        libvmaf="libvmaf" in filters,
        libvmaf_cuda="libvmaf_cuda" in filters,
        loopback_decoder=_detect_loopback_decoder(path, decoders),
        hwaccels=hwaccels,
        filters=filters,
        encoders=encoders,
        scale_vt="scale_vt" in filters,
        scale_cuda="scale_cuda" in filters,
        videotoolbox_hwaccel="videotoolbox" in hwaccels,
        cuda_hwaccel="cuda" in hwaccels,
        videotoolbox_prio_speed=encoder_help_has_option(vt_help, "prio_speed"),
    )


def detect_analysis_capabilities(ffmpeg_path: Path) -> AnalysisCapabilities:
    try:
        stat = ffmpeg_path.stat()
        size, mtime_ns = stat.st_size, stat.st_mtime_ns
    except OSError:
        size, mtime_ns = None, None
    return _detect_analysis_capabilities_cached(str(ffmpeg_path), size, mtime_ns)


def format_analysis_capability_report(capabilities: AnalysisCapabilities) -> str:
    def yn(value: bool) -> str:
        return "yes" if value else "no"

    rows = (
        ("CPU VMAF", yn(capabilities.libvmaf)),
        ("VideoToolbox", yn(capabilities.videotoolbox_hwaccel)),
        ("scale_vt", yn(capabilities.scale_vt)),
        ("CUDA VMAF filter (not enabled for v1)", yn(capabilities.libvmaf_cuda)),
        ("Loopback decoder", yn(capabilities.loopback_decoder)),
    )
    width = max(len(label) for label, _ in rows)
    lines = ["Analysis acceleration capabilities:", ""]
    lines.extend(f"{label:<{width}}  {value}" for label, value in rows)
    return "\n".join(lines)


def source_decode_args(acceleration: str) -> list[str]:
    if acceleration == SOURCE_DECODE_SOFTWARE:
        return []
    if acceleration == SOURCE_DECODE_VIDEOTOOLBOX:
        return ["-hwaccel", "videotoolbox"]
    if acceleration == SOURCE_DECODE_CUDA:
        return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if acceleration == SOURCE_DECODE_QSV:
        return ["-hwaccel", "qsv"]
    if acceleration == SOURCE_DECODE_D3D11VA:
        return ["-hwaccel", "d3d11va"]
    if acceleration == SOURCE_DECODE_VAAPI:
        return ["-hwaccel", "vaapi"]
    raise ValueError(f"Unsupported analysis decode acceleration: {acceleration}")


def _faster_numeric_preset(preset: str, *, faster_by: int, lo: int, hi: int) -> str:
    value = int(preset)
    return str(min(hi, max(lo, value + faster_by)))


def coarse_encoder_preset(encoder_name: str, production_preset: str | None) -> str | None:
    if encoder_name.endswith("_videotoolbox"):
        return None
    if not production_preset:
        if encoder_name == "libx265":
            return "fast"
        if encoder_name == "libsvtav1":
            return "8"
        if encoder_name.endswith("_nvenc"):
            return "p4"
        if encoder_name.endswith("_qsv"):
            return "fast"
        return None

    if encoder_name == "libx265":
        mapping = {
            "placebo": "fast",
            "veryslow": "fast",
            "slower": "fast",
            "slow": "fast",
            "medium": "fast",
            "fast": "faster",
            "faster": "veryfast",
            "veryfast": "superfast",
            "superfast": "ultrafast",
            "ultrafast": "ultrafast",
        }
        return mapping.get(production_preset, "fast")

    if encoder_name == "libsvtav1":
        try:
            return _faster_numeric_preset(production_preset, faster_by=3, lo=0, hi=12)
        except ValueError:
            return "8"

    if encoder_name.endswith("_nvenc") and production_preset.startswith("p") and production_preset[1:].isdigit():
        quality = int(production_preset[1:])
        return f"p{max(1, quality - 2)}"

    if encoder_name.endswith("_qsv"):
        mapping = {
            "veryslow": "fast",
            "slower": "fast",
            "slow": "fast",
            "medium": "fast",
            "fast": "faster",
            "faster": "veryfast",
            "veryfast": "veryfast",
        }
        return mapping.get(production_preset, "fast")

    return production_preset


def coarse_encoder_extra_args(encoder_name: str, capabilities: AnalysisCapabilities) -> tuple[str, ...]:
    if encoder_name == "hevc_videotoolbox" and capabilities.videotoolbox_prio_speed:
        return ("-prio_speed", "1")
    return ()


def _select_source_decode(
    capabilities: AnalysisCapabilities,
    policy: AnalysisDecodePolicy,
    vmaf_backend: VmafBackend,
) -> str:
    if policy == AnalysisDecodePolicy.SOFTWARE:
        return SOURCE_DECODE_SOFTWARE
    if vmaf_backend == VmafBackend.CUDA and capabilities.cuda_hwaccel:
        return SOURCE_DECODE_CUDA
    if capabilities.videotoolbox_hwaccel:
        return SOURCE_DECODE_VIDEOTOOLBOX
    if capabilities.cuda_hwaccel:
        return SOURCE_DECODE_CUDA
    return SOURCE_DECODE_SOFTWARE


def software_source_plan(plan: AnalysisExecutionPlan, *, reason: str) -> AnalysisExecutionPlan:
    return AnalysisExecutionPlan(
        tier=plan.tier,
        source_decode_acceleration=SOURCE_DECODE_SOFTWARE,
        encoder_name=plan.encoder_name,
        encoder_preset=plan.encoder_preset,
        encoder_extra_args=plan.encoder_extra_args,
        two_pass=plan.two_pass,
        vmaf_backend=plan.vmaf_backend,
        vmaf_threads=plan.vmaf_threads,
        vmaf_subsample=plan.vmaf_subsample,
        use_loopback=plan.use_loopback,
        fallback_reason=reason,
    )


def cpu_vmaf_plan(plan: AnalysisExecutionPlan, *, reason: str) -> AnalysisExecutionPlan:
    decode = (
        SOURCE_DECODE_SOFTWARE
        if plan.source_decode_acceleration == SOURCE_DECODE_CUDA
        else plan.source_decode_acceleration
    )
    return AnalysisExecutionPlan(
        tier=plan.tier,
        source_decode_acceleration=decode,
        encoder_name=plan.encoder_name,
        encoder_preset=plan.encoder_preset,
        encoder_extra_args=plan.encoder_extra_args,
        two_pass=plan.two_pass,
        vmaf_backend=VmafBackend.CPU,
        vmaf_threads=plan.vmaf_threads,
        vmaf_subsample=plan.vmaf_subsample,
        use_loopback=False if plan.vmaf_backend == VmafBackend.CUDA else plan.use_loopback,
        fallback_reason=reason,
    )


def legacy_loopback_plan(plan: AnalysisExecutionPlan, *, reason: str) -> AnalysisExecutionPlan:
    return AnalysisExecutionPlan(
        tier=plan.tier,
        source_decode_acceleration=plan.source_decode_acceleration,
        encoder_name=plan.encoder_name,
        encoder_preset=plan.encoder_preset,
        encoder_extra_args=plan.encoder_extra_args,
        two_pass=plan.two_pass,
        vmaf_backend=plan.vmaf_backend,
        vmaf_threads=plan.vmaf_threads,
        vmaf_subsample=plan.vmaf_subsample,
        use_loopback=False,
        fallback_reason=reason,
    )


def build_analysis_execution_plan(
    *,
    tier: AnalysisTier,
    encoder_info: EncoderInfo,
    production_preset: str | None,
    production_two_pass: bool,
    capabilities: AnalysisCapabilities,
    decode_policy: AnalysisDecodePolicy = AnalysisDecodePolicy.AUTO,
    vmaf_backend: VmafBackend = VmafBackend.CPU,
    active_cpu_vmaf_jobs: int = 1,
    enable_loopback: bool = False,
    coarse_vmaf_subsample: int = COARSE_VMAF_SUBSAMPLE,
    exact_vmaf_subsample: int = EXACT_VMAF_SUBSAMPLE,
) -> AnalysisExecutionPlan:
    encoder_name = encoder_info.encoder_name
    if encoder_name == "av1_videotoolbox":
        raise ValueError("av1_videotoolbox is not a supported analysis encoder.")

    source_decode = _select_source_decode(capabilities, decode_policy, vmaf_backend)
    if encoder_info.backend == BackendChoice.VIDEOTOOLBOX and encoder_name.startswith("av1_"):
        encoder_name = "libsvtav1"

    if tier == AnalysisTier.COARSE:
        return AnalysisExecutionPlan(
            tier=tier,
            source_decode_acceleration=source_decode,
            encoder_name=encoder_name,
            encoder_preset=coarse_encoder_preset(encoder_name, production_preset),
            encoder_extra_args=coarse_encoder_extra_args(encoder_name, capabilities),
            two_pass=False,
            vmaf_backend=vmaf_backend,
            vmaf_threads=vmaf_thread_budget(active_cpu_vmaf_jobs),
            vmaf_subsample=coarse_vmaf_subsample,
            use_loopback=bool(enable_loopback and capabilities.loopback_decoder),
        )

    return AnalysisExecutionPlan(
        tier=tier,
        source_decode_acceleration=source_decode,
        encoder_name=encoder_name,
        encoder_preset=production_preset,
        encoder_extra_args=(),
        two_pass=bool(production_two_pass and encoder_info.supports_two_pass),
        vmaf_backend=vmaf_backend,
        vmaf_threads=vmaf_thread_budget(active_cpu_vmaf_jobs),
        vmaf_subsample=exact_vmaf_subsample,
        use_loopback=bool(enable_loopback and capabilities.loopback_decoder),
    )
