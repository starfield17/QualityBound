from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from core.models import BackendChoice, CodecChoice, EncoderInfo
from core.ffmpeg.subprocess import noninteractive_run_kwargs


# Ordered by preference: hardware encoders (NVENC, QSV, AMF, VideoToolbox) are
# tried before the universal CPU fallback.
AUTO_BACKEND_PRIORITY: tuple[BackendChoice, ...] = (
    BackendChoice.NVENC,
    BackendChoice.QSV,
    BackendChoice.AMF,
    BackendChoice.VIDEOTOOLBOX,
    BackendChoice.CPU,
)

ENCODER_CANDIDATES: dict[CodecChoice, dict[BackendChoice, str]] = {
    CodecChoice.HEVC: {
        BackendChoice.NVENC: "hevc_nvenc",
        BackendChoice.QSV: "hevc_qsv",
        BackendChoice.AMF: "hevc_amf",
        BackendChoice.VIDEOTOOLBOX: "hevc_videotoolbox",
        BackendChoice.CPU: "libx265",
    },
    CodecChoice.AV1: {
        BackendChoice.NVENC: "av1_nvenc",
        BackendChoice.QSV: "av1_qsv",
        BackendChoice.AMF: "av1_amf",
        BackendChoice.CPU: "libsvtav1",
    },
}

# Hardcoded preset lists used when parsing the encoder's help output fails.
# Based on published encoder docs; may drift if ffmpeg adds or removes presets.
FALLBACK_PRESET_CHOICES: dict[str, tuple[str, ...]] = {
    "libx265": (
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
        "placebo",
    ),
    "hevc_nvenc": ("p1", "p2", "p3", "p4", "p5", "p6", "p7"),
    "av1_nvenc": ("p1", "p2", "p3", "p4", "p5", "p6", "p7"),
    "hevc_qsv": ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
    "av1_qsv": ("veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
}
QUALITY_PRESET_KEYWORD = "quality"
PRESET_LINE_RE = re.compile(r"^\s*-preset(?:\s|$)")
OPTION_LINE_RE = re.compile(r"^\s*-[A-Za-z0-9]")
PRESET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]+$")
HWACCEL_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.+\-]*$")


def parse_hwaccels(output: str) -> set[str]:
    """Parse the accelerator names printed by ``ffmpeg -hwaccels``."""
    hwaccels: set[str] = set()
    for line in output.splitlines():
        normalized = line.strip().lower()
        if not normalized or normalized.rstrip(":") == "hardware acceleration methods":
            continue
        if HWACCEL_TOKEN_RE.fullmatch(normalized):
            hwaccels.add(normalized)
    return hwaccels


def list_available_hwaccels(ffmpeg_path: Path) -> set[str]:
    proc = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-hwaccels"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **noninteractive_run_kwargs(),
    )
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    return parse_hwaccels(output)


def list_available_encoders(ffmpeg_path: Path) -> set[str]:
    proc = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **noninteractive_run_kwargs(),
    )
    encoders: set[str] = set()
    pattern = re.compile(r"^\s*[A-Z\.]{6}\s+([^\s]+)")
    for line in proc.stdout.splitlines():
        match = pattern.match(line)
        if match:
            encoders.add(match.group(1))
    return encoders


def iter_codec_candidates(
    codec: CodecChoice,
) -> Iterable[tuple[BackendChoice, str]]:
    backend_map = ENCODER_CANDIDATES[codec]
    for backend in AUTO_BACKEND_PRIORITY:
        encoder_name = backend_map.get(backend)
        if encoder_name is not None:
            yield backend, encoder_name


def _fallback_preset_choices(encoder_name: str) -> list[str]:
    return list(FALLBACK_PRESET_CHOICES.get(encoder_name, ()))


def _run_encoder_help(ffmpeg_path: Path, encoder_name: str) -> str:
    proc = subprocess.run(
        [str(ffmpeg_path), "-hide_banner", "-h", f"encoder={encoder_name}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **noninteractive_run_kwargs(),
    )
    return "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def _looks_like_preset_value(token: str) -> bool:
    normalized = token.strip()
    if not normalized or normalized in {"default", "from", "to"}:
        return False
    if normalized.startswith("<") or normalized.startswith("("):
        return False
    return PRESET_TOKEN_RE.fullmatch(normalized) is not None


def _extract_preset_choices(help_text: str) -> list[str]:
    lines = help_text.splitlines()
    start_index = next((index for index, line in enumerate(lines) if PRESET_LINE_RE.search(line)), -1)
    if start_index < 0:
        return []

    choices: list[str] = []
    for line in lines[start_index + 1 :]:
        if OPTION_LINE_RE.match(line):
            break
        token = line.strip().split(maxsplit=1)[0] if line.strip() else ""
        if _looks_like_preset_value(token) and token not in choices:
            choices.append(token)
    return choices


def _ffmpeg_file_identity(ffmpeg_path: Path) -> tuple[int, int, int]:
    """Return the file fields that invalidate in-process encoder-help caches."""
    try:
        stat = ffmpeg_path.stat()
    except OSError:
        return (-1, -1, -1)
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


@lru_cache(maxsize=64)
def _cached_runtime_preset_choices(
    ffmpeg_path: Path,
    encoder_name: str,
    _ffmpeg_identity: tuple[int, int, int],
) -> tuple[str, ...]:
    output = _run_encoder_help(ffmpeg_path, encoder_name)
    return tuple(_extract_preset_choices(output))


def preset_choices_for_encoder(ffmpeg_path: Path, encoder_name: str) -> list[str]:
    if encoder_name.endswith("_videotoolbox"):
        return []
    try:
        choices = list(
            _cached_runtime_preset_choices(
                ffmpeg_path,
                encoder_name,
                _ffmpeg_file_identity(ffmpeg_path),
            )
        )
    except (OSError, subprocess.SubprocessError):
        # Capability discovery should still report a usable encoder when its
        # optional help probe cannot be run.  The static list below is the same
        # safe fallback used when FFmpeg returns help without a preset section.
        choices = []
    if choices:
        return choices
    return _fallback_preset_choices(encoder_name)


def is_valid_preset(ffmpeg_path: Path, encoder_name: str, preset: str) -> bool:
    normalized = preset.strip()
    if not normalized:
        return False
    return normalized in preset_choices_for_encoder(ffmpeg_path, encoder_name)


def _quality_preset_from_choices(choices: list[str]) -> str | None:
    for choice in choices:
        if QUALITY_PRESET_KEYWORD in choice.lower():
            return choice
    return None


def default_preset_for_encoder(encoder_name: str, ffmpeg_path: Path | None = None) -> str | None:
    if encoder_name == "libx265":
        return "slow"
    if encoder_name == "libsvtav1":
        return "5"
    if encoder_name in {"hevc_nvenc", "av1_nvenc"}:
        return "p6"
    if encoder_name in {"hevc_qsv", "av1_qsv"}:
        return "slow"
    if encoder_name == "hevc_videotoolbox":
        return None
    if encoder_name in {"hevc_amf", "av1_amf"} and ffmpeg_path is not None:
        return _quality_preset_from_choices(preset_choices_for_encoder(ffmpeg_path, encoder_name))
    return None


def _encoder_info(
    codec: CodecChoice,
    backend: BackendChoice,
    encoder_name: str,
    ffmpeg_path: Path | None,
) -> EncoderInfo:
    return EncoderInfo(
        codec=codec,
        backend=backend,
        encoder_name=encoder_name,
        # Only x265 exposes a reliable two-pass mode through ffmpeg.
        supports_two_pass=encoder_name == "libx265",
        default_preset=default_preset_for_encoder(encoder_name, ffmpeg_path),
    )


def _runtime_candidates_for_codec(
    codec: CodecChoice,
    runtime_capabilities: dict | None,
) -> dict[BackendChoice, str] | None:
    if runtime_capabilities is None:
        return None
    candidates: dict[BackendChoice, str] = {}
    for backend in ENCODER_CANDIDATES[codec]:
        entry = encoder_capability_for_backend(runtime_capabilities, codec, backend)
        if entry is None:
            continue
        encoder_name = entry.get("encoder")
        if isinstance(encoder_name, str) and encoder_name:
            candidates[backend] = encoder_name
    return candidates


def encoder_capability_for_backend(
    runtime_capabilities: dict | None,
    codec: CodecChoice,
    backend: BackendChoice,
) -> dict[str, object] | None:
    """Return the validated runtime entry for one codec/backend pair.

    Capability payloads are persisted as JSON dictionaries, so callers should
    use this query instead of reaching through ``codecs`` themselves.  The
    expected encoder mapping is checked here as a defense against stale or
    hand-edited payloads.
    """
    if runtime_capabilities is None or backend not in ENCODER_CANDIDATES[codec]:
        return None
    codecs = runtime_capabilities.get("codecs")
    if not isinstance(codecs, dict):
        return None
    items = codecs.get(codec.value)
    if not isinstance(items, list):
        return None
    expected_encoder = ENCODER_CANDIDATES[codec][backend]
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("backend", "")).strip() != backend.value:
            continue
        if str(item.get("encoder", "")).strip() != expected_encoder:
            continue
        return dict(item)
    return None


def available_backends_for_codec(
    runtime_capabilities: dict | None,
    codec: CodecChoice,
) -> tuple[BackendChoice, ...]:
    """Return the usable concrete backends recorded for ``codec``."""
    if runtime_capabilities is None:
        return ()
    return tuple(
        backend
        for backend in AUTO_BACKEND_PRIORITY
        if encoder_capability_for_backend(runtime_capabilities, codec, backend) is not None
    )


def encoder_name_for_backend(
    runtime_capabilities: dict | None,
    codec: CodecChoice,
    backend: BackendChoice,
) -> str | None:
    """Return the concrete encoder recorded for a usable backend."""
    entry = encoder_capability_for_backend(runtime_capabilities, codec, backend)
    encoder_name = entry.get("encoder") if entry is not None else None
    return encoder_name if isinstance(encoder_name, str) and encoder_name else None


def preset_choices_from_capabilities(
    runtime_capabilities: dict | None,
    codec: CodecChoice,
    backend: BackendChoice,
) -> list[str]:
    """Return preset choices from a completed capability snapshot.

    Missing ``preset_choices`` is tolerated for in-memory legacy snapshots and
    uses the deterministic encoder fallback.  Persisted legacy snapshots are
    rejected by the cache schema validator and are rebuilt before reaching the
    GUI.  A present but malformed value is treated as unavailable.
    """
    entry = encoder_capability_for_backend(runtime_capabilities, codec, backend)
    if entry is None:
        return []
    raw_choices = entry.get("preset_choices")
    if raw_choices is None:
        encoder_name = entry.get("encoder")
        return _fallback_preset_choices(encoder_name) if isinstance(encoder_name, str) else []
    if not isinstance(raw_choices, list):
        return []
    return [choice for choice in raw_choices if isinstance(choice, str) and choice]


def resolve_encoder(
    codec: CodecChoice,
    backend: BackendChoice,
    available_encoders: set[str],
    ffmpeg_path: Path | None = None,
    runtime_capabilities: dict | None = None,
) -> EncoderInfo:
    backend_map = ENCODER_CANDIDATES[codec]
    runtime_candidates = _runtime_candidates_for_codec(codec, runtime_capabilities)

    if backend == BackendChoice.AUTO:
        if runtime_candidates is not None:
            for candidate_backend in AUTO_BACKEND_PRIORITY:
                encoder_name = runtime_candidates.get(candidate_backend)
                if encoder_name:
                    return _encoder_info(codec, candidate_backend, encoder_name, ffmpeg_path)
            raise RuntimeError(f"No usable {codec.value} encoder was found on this machine.")

        for candidate_backend, encoder_name in iter_codec_candidates(codec):
            if encoder_name in available_encoders:
                return _encoder_info(codec, candidate_backend, encoder_name, ffmpeg_path)
        raise RuntimeError(f"No available {codec.value} encoder was found in the current FFmpeg build.")

    encoder_name = backend_map.get(backend)
    if encoder_name is None:
        raise RuntimeError(
            f"Backend {backend.value} does not support codec {codec.value}."
        )
    if runtime_candidates is not None:
        if runtime_candidates.get(backend) == encoder_name:
            return _encoder_info(codec, backend, encoder_name, ffmpeg_path)
        raise RuntimeError(f"Requested encoder {encoder_name} is not usable with the current FFmpeg/hardware.")

    if encoder_name not in available_encoders:
        raise RuntimeError(f"Requested encoder {encoder_name} is not available in the current FFmpeg build.")
    return _encoder_info(codec, backend, encoder_name, ffmpeg_path)
