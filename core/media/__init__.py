"""Media discovery, bitrate, system power, and output-file contracts."""

from core.media.bitrate import (
    DEFAULT_RATIO,
    choose_ratio,
    compute_target_video_bitrate,
    human_kbps,
    kbps_to_bps,
)
from core.media.discovery import VIDEO_EXTENSIONS, collect_video_files
from core.media.metadata import infer_bit_depth_from_pix_fmt
from core.media.skipped import (
    group_skipped_output_pairs,
    is_eligible_skipped_item,
    publish_skipped_source,
    publish_skipped_sources,
)
from core.media.space_savings import (
    SpaceSavingsItem,
    SpaceSavingsOutcome,
    SpaceSavingsSummary,
    calculate_space_savings,
)
from core.media.system_power import (
    PostEncodeAction,
    SystemPowerResult,
    execute_power_action,
    execute_system_shutdown,
    execute_system_sleep,
    parse_post_encode_action,
)
from core.media.validation import validate_plan_item, validate_unique_output_paths

__all__ = [
    "DEFAULT_RATIO",
    "PostEncodeAction",
    "SpaceSavingsItem",
    "SpaceSavingsOutcome",
    "SpaceSavingsSummary",
    "SystemPowerResult",
    "VIDEO_EXTENSIONS",
    "calculate_space_savings",
    "choose_ratio",
    "collect_video_files",
    "compute_target_video_bitrate",
    "execute_power_action",
    "execute_system_shutdown",
    "execute_system_sleep",
    "group_skipped_output_pairs",
    "human_kbps",
    "infer_bit_depth_from_pix_fmt",
    "is_eligible_skipped_item",
    "kbps_to_bps",
    "parse_post_encode_action",
    "publish_skipped_source",
    "publish_skipped_sources",
    "validate_plan_item",
    "validate_unique_output_paths",
]
