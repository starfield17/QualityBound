"""Public Smart-analysis and size-decision contract.

Core implementation modules import the concrete owner module. CLI and GUI
adapters use this deliberately small package API.
"""

from core.smart.bitrate import resolve_max_output_ratio
from core.smart.decisions import (
    accept_rejected_output,
    build_decision_options,
    constraint_policy_from_size_blocked,
    discard_rejected_output,
    prepare_size_miss_retry,
    reselect_after_quality_decision,
    size_blocked_from_constraint_policy,
)
from core.smart.profiles import (
    analysis_profiles_from_config,
    bind_analysis_profile,
    parse_analysis_profile_name,
)
from core.smart.receipts import delete_analysis_receipt
from core.smart.vmaf import VMAF_PRODUCTION_MODELS, probe_vmaf_runtime
from core.smart.workflow import analyze_quality
from core.smart.v2.receipts import delete_receipt as delete_segmented_analysis_receipt


__all__ = [
    "VMAF_PRODUCTION_MODELS",
    "accept_rejected_output",
    "analysis_profiles_from_config",
    "analyze_quality",
    "bind_analysis_profile",
    "build_decision_options",
    "constraint_policy_from_size_blocked",
    "delete_analysis_receipt",
    "delete_segmented_analysis_receipt",
    "discard_rejected_output",
    "parse_analysis_profile_name",
    "prepare_size_miss_retry",
    "probe_vmaf_runtime",
    "reselect_after_quality_decision",
    "resolve_max_output_ratio",
    "size_blocked_from_constraint_policy",
]
