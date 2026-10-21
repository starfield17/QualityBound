"""FFmpeg discovery, capability, probing, and command-building contracts."""

from core.ffmpeg.capabilities import (
    ENCODER_CAPABILITIES_SCHEMA_VERSION,
    detect_encoder_capabilities,
    ensure_encoder_capabilities,
    is_encoder_capability_cache_valid,
    smoke_test_encoder,
)
from core.ffmpeg.commands import (
    build_encode_commands,
    build_input_acceleration_args,
    build_video_args,
)
from core.ffmpeg.discovery import discover_ffmpeg_tools, find_binary
from core.ffmpeg.encoders import (
    ENCODER_CANDIDATES,
    available_backends_for_codec,
    default_preset_for_encoder,
    is_valid_preset,
    iter_codec_candidates,
    list_available_encoders,
    list_available_hwaccels,
    parse_hwaccels,
    preset_choices_for_encoder,
    preset_choices_from_capabilities,
    resolve_encoder,
)
from core.ffmpeg.probe import probe_media_info
from core.ffmpeg.subprocess import (
    hidden_popen_kwargs,
    hidden_process_creationflags,
    noninteractive_run_kwargs,
    terminate_process,
)

__all__ = [
    "ENCODER_CANDIDATES",
    "ENCODER_CAPABILITIES_SCHEMA_VERSION",
    "available_backends_for_codec",
    "build_encode_commands",
    "build_input_acceleration_args",
    "build_video_args",
    "default_preset_for_encoder",
    "detect_encoder_capabilities",
    "discover_ffmpeg_tools",
    "ensure_encoder_capabilities",
    "find_binary",
    "hidden_popen_kwargs",
    "hidden_process_creationflags",
    "is_encoder_capability_cache_valid",
    "is_valid_preset",
    "iter_codec_candidates",
    "list_available_encoders",
    "list_available_hwaccels",
    "noninteractive_run_kwargs",
    "parse_hwaccels",
    "preset_choices_for_encoder",
    "preset_choices_from_capabilities",
    "probe_media_info",
    "resolve_encoder",
    "smoke_test_encoder",
    "terminate_process",
]
