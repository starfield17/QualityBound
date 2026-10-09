from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class CodecChoice(str, Enum):
    HEVC = "hevc"
    AV1 = "av1"


class CompressionMode(str, Enum):
    SMART = "smart"
    FIXED_BITRATE = "fixed_bitrate"


class SmartAlgorithm(str, Enum):
    V1 = "v1"
    V2_EXPERIMENTAL = "v2_experimental"


class SizeBlockedPolicy(str, Enum):
    RELAX_SIZE = "relax_size"
    RELAX_QUALITY = "relax_quality"
    ASK = "ask"


class QualityUnreachablePolicy(str, Enum):
    SKIP = "skip"
    ASK = "ask"


class SkippedOutputPolicy(str, Enum):
    COPY = "copy"
    ASK = "ask"
    IGNORE = "ignore"


class SkipOrigin(str, Enum):
    PLANNING = "planning"
    SMART_ANALYSIS = "smart_analysis"
    SMART_ANALYSIS_DECISION = "smart_analysis_decision"
    SMART_PREDICTED_OVERSIZE = "smart_predicted_oversize"
    SIZE_MISS_DISCARD = "size_miss_discard"


class AnalysisProfileName(str, Enum):
    FAST = "fast"
    BALANCE = "balance"
    PRECISE = "precise"


class SkippedOutputOutcome(str, Enum):
    COPIED = "copied"
    IGNORED = "ignored"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AnalysisProfileSettings:
    whole_video_max_sec: float = 20.0
    scout_duration_sec: float = 2.0
    scout_multiplier: int = 4
    scout_max_windows: int = 32
    sample_duration_sec: float = 5.0
    sample_count_under_10m: int = 4
    sample_count_10_to_60m: int = 5
    sample_count_60_to_180m: int = 6
    sample_count_over_180m: int = 6
    holdout_window_count: int = 2
    holdout_window_count_over_180m: int = 2
    coarse_max_candidates: int = 4
    exact_max_candidates: int = 3
    coarse_vmaf_subsample: int = 3
    exact_vmaf_subsample: int = 1
    min_search_tolerance_bps: int = 50_000
    search_tolerance_ratio: float = 0.03
    max_refinement_rounds: int = 2
    preferred_vmaf_margin: float = 0.4
    search_min_windows: int = 3
    search_max_windows: int = 6
    holdout_target_min: int = 1
    holdout_target_max: int = 2
    reserve_window_count: int = 2
    quality_confidence_band: float = 0.7


class BackendChoice(str, Enum):
    AUTO = "auto"
    CPU = "cpu"
    NVENC = "nvenc"
    QSV = "qsv"
    AMF = "amf"
    VIDEOTOOLBOX = "videotoolbox"


class DecodeAcceleration(str, Enum):
    SOFTWARE = "software"
    VIDEOTOOLBOX = "videotoolbox"


class AudioMode(str, Enum):
    COPY = "copy"
    AAC = "aac"


class ContainerChoice(str, Enum):
    MKV = "mkv"
    MP4 = "mp4"


class VmafBackend(str, Enum):
    CPU = "cpu"
    CUDA = "cuda"


class VmafViewingContext(str, Enum):
    HIGH_FIDELITY = "high_fidelity"
    STANDARD_DISPLAY = "standard_display"


class OperationCancelledError(RuntimeError):
    """Raised when a running planning or encode task is cancelled."""


@dataclass(slots=True)
class VideoFileItem:
    path: Path
    relative_path: Path


@dataclass(slots=True)
class MediaInfo:
    path: Path
    duration: float
    format_bitrate_bps: int
    video_bitrate_bps: int
    audio_bitrate_bps: int
    width: Optional[int]
    height: Optional[int]
    fps: Optional[float]
    video_codec: str
    audio_codec: Optional[str]
    audio_stream_count: int = 0
    pix_fmt: Optional[str] = None
    bit_depth: Optional[int] = None
    color_transfer: Optional[str] = None


@dataclass(slots=True)
class EncodeOptions:
    codec: CodecChoice = CodecChoice.HEVC
    compression_mode: CompressionMode = CompressionMode.SMART
    backend: BackendChoice = BackendChoice.AUTO
    decode_acceleration: DecodeAcceleration = DecodeAcceleration.SOFTWARE
    # None means choose the default compression ratio for the selected codec.
    ratio: Optional[float] = None
    min_vmaf: float = 90.0
    viewing_context: VmafViewingContext = VmafViewingContext.HIGH_FIDELITY
    # None means choose the smart output ratio for the selected codec.
    max_output_ratio: Optional[float] = None
    min_video_kbps: int = 250
    # 0 means no upper cap on video bitrate.
    max_video_kbps: int = 0
    container: ContainerChoice = ContainerChoice.MP4
    audio_mode: AudioMode = AudioMode.COPY
    audio_bitrate: str = "128k"
    copy_subtitles: bool = True
    copy_external_subtitles: bool = True
    two_pass: bool = False
    # Encoder-specific speed/quality preset name, such as "slow" or "p6".
    encoder_preset: Optional[str] = None
    # yuv420p keeps output broadly playable on older players and devices.
    pix_fmt: str = "yuv420p"
    # VBV rate-control multipliers; defaults keep bufsize comfortably above maxrate.
    maxrate_factor: float = 1.25
    bufsize_factor: float = 4.0
    overwrite: bool = False
    recursive: bool = False
    dry_run: bool = False
    size_blocked_policy: SizeBlockedPolicy = SizeBlockedPolicy.RELAX_SIZE
    quality_unreachable_policy: QualityUnreachablePolicy = QualityUnreachablePolicy.SKIP
    skipped_output_policy: SkippedOutputPolicy = SkippedOutputPolicy.COPY
    analysis_profile: AnalysisProfileName = AnalysisProfileName.BALANCE
    analysis_settings: AnalysisProfileSettings = field(default_factory=AnalysisProfileSettings)
    smart_algorithm: SmartAlgorithm = SmartAlgorithm.V1


@dataclass(slots=True)
class EncodeRequest:
    """A source and selected settings awaiting a concrete execution binding.

    input_root identifies a folder import; None identifies explicit files.
    """
    file_item: VideoFileItem
    options: EncodeOptions
    output_dir: Optional[Path] = None
    input_root: Optional[Path] = None


@dataclass(slots=True)
class EncoderInfo:
    codec: CodecChoice
    backend: BackendChoice
    encoder_name: str
    supports_two_pass: bool
    default_preset: Optional[str]


@dataclass(slots=True)
class EncodePlanItem:
    source_path: Path
    output_path: Path
    media_info: Optional[MediaInfo]
    encoder_info: Optional[EncoderInfo]
    options: EncodeOptions
    target_video_bitrate_bps: int = 0
    warnings: list[str] = field(default_factory=list)
    skip_reason: Optional[str] = None
    quality_search_result: Optional["QualitySearchResult"] = None
    segmented_analysis_result: Optional["SegmentedAnalysisResult"] = None
    ffprobe_path: Optional[Path] = None
    # Runtime correction only; never changes the per-shot bitrate ceiling.
    segmented_video_budget_bytes: Optional[int] = None


@dataclass(slots=True)
class EncodePlan:
    items: list[EncodePlanItem]
    ffmpeg_path: Path
    ffprobe_path: Path
    input_root: Path
    output_root: Path
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class EncodeResult:
    source_path: Path
    output_path: Path
    success: bool
    return_code: int = 0
    commands: list[list[str]] = field(default_factory=list)
    log_path: Optional[Path] = None
    error_message: Optional[str] = None
    skipped: bool = False
    skip_origin: Optional[SkipOrigin] = None
    skipped_output_outcome: Optional[SkippedOutputOutcome] = None
    needs_decision: bool = False
    effective_min_vmaf: Optional[float] = None
    effective_max_output_ratio: Optional[float] = None
    rejected_output_path: Optional[Path] = None
    actual_output_bytes: Optional[int] = None
    allowed_output_bytes: Optional[int] = None
    copied_external_subtitle_paths: list[Path] = field(default_factory=list)
    external_subtitle_warnings: list[str] = field(default_factory=list)
    quality_search_result: Optional["QualitySearchResult"] = None
    segmented_analysis_result: Optional["SegmentedAnalysisResult"] = None


class QualitySearchStatus(str, Enum):
    FOUND = "found"
    CONSTRAINT_UNSATISFIED = "constraint_unsatisfied"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ConstraintFailureKind(str, Enum):
    SIZE_BLOCKED = "size_blocked"
    QUALITY_UNREACHABLE = "quality_unreachable"
    MEDIA_BUDGET_TOO_SMALL = "media_budget_too_small"


class ConstraintPolicy(str, Enum):
    FAIL = "fail"
    RELAX_SIZE = "relax_size"
    RELAX_QUALITY = "relax_quality"


class DecisionActionCode(str, Enum):
    RELAX_SIZE = "relax_size"
    RELAX_QUALITY = "relax_quality"
    CHANGE_MEDIA_BUDGET = "change_media_budget"
    REANALYZE = "reanalyze"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class VmafRuntimeSupport:
    backend: VmafBackend
    model: str
    runnable: bool
    error_message: Optional[str] = None


@dataclass(slots=True)
class QualityCandidateResult:
    video_bitrate_bps: int
    segment_vmaf: list[float] = field(default_factory=list)
    min_vmaf: float = 0.0
    # Smart analysis measures each encoded sample instead of assuming that
    # the requested -b:v value is the bitrate the encoder will produce.  The
    # defaults keep this result backwards-compatible with callers that only
    # provide VMAF scores.
    encoded_bytes: list[int] = field(default_factory=list)
    encoded_durations_sec: list[float] = field(default_factory=list)
    observed_video_bitrate_bps: int = 0
    predicted_output_bytes: Optional[int] = None
    predicted_output_ratio: Optional[float] = None
    segment_mean_vmaf: list[float] = field(default_factory=list)
    segment_p10_vmaf: list[float] = field(default_factory=list)
    segment_worst_1s_vmaf: list[float] = field(default_factory=list)
    segment_quality_scores: list[float] = field(default_factory=list)
    observed_window_bitrates: list[int] = field(default_factory=list)
    size_prediction: Optional["SizePrediction"] = None
    rd_ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class SizePrediction:
    mean_video_bitrate_bps: int
    upper_video_bitrate_bps: int
    predicted_output_bytes: int
    predicted_output_ratio: Optional[float]
    uncertainty: float
    method: str


@dataclass(slots=True)
class QualitySearchResult:
    status: QualitySearchStatus
    encoder_name: str
    backend: BackendChoice
    candidates: list[QualityCandidateResult] = field(default_factory=list)
    selected_video_bitrate_bps: int = 0
    min_vmaf: Optional[float] = None
    predicted_output_bytes: Optional[int] = None
    predicted_output_ratio: Optional[float] = None
    required_output_ratio: Optional[float] = None
    required_video_bitrate_bps: int = 0
    best_size_fitting_candidate_bps: int = 0
    best_size_fitting_vmaf: Optional[float] = None
    max_output_bytes: Optional[int] = None
    failure_kind: Optional[ConstraintFailureKind] = None
    measurement_fingerprint: str = ""
    fingerprint: str = ""
    reason: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.status == QualitySearchStatus.FOUND


@dataclass(frozen=True, slots=True)
class ShotRange:
    start_frame: int
    end_frame: int

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(slots=True)
class ShotCandidate:
    bitrate_bps: int
    mean_vmaf: float
    worst_1s_vmaf: float
    predicted_video_bytes: int
    measured_frames: int
    whole_shot: bool = False
    artifact: Optional[Path] = None
    artifact_hash: Optional[str] = None
    holdout_verified: bool = False
    full_verified: bool = False


@dataclass(slots=True)
class ShotAnalysis:
    shot: ShotRange
    search_windows: list[ShotRange] = field(default_factory=list)
    holdout_window: Optional[ShotRange] = None
    candidates: list[ShotCandidate] = field(default_factory=list)


@dataclass(slots=True)
class SegmentedAnalysisResult:
    status: QualitySearchStatus
    encoder_name: str
    backend: BackendChoice
    shots: list[ShotAnalysis] = field(default_factory=list)
    selected: list[ShotCandidate] = field(default_factory=list)
    fps: float = 0.0
    fps_rational: str = ""
    source_start_sec: float = 0.0
    source_frames: int = 0
    predicted_mean_vmaf: Optional[float] = None
    predicted_output_bytes: Optional[int] = None
    max_output_bytes: int = 0
    video_budget_bytes: int = 0
    required_output_ratio: Optional[float] = None
    best_size_fitting_vmaf: Optional[float] = None
    failure_kind: Optional[ConstraintFailureKind] = None
    measurement_fingerprint: str = ""
    approximate: bool = False
    reason: Optional[str] = None
    final_mean_vmaf: Optional[float] = None
    final_worst_1s_vmaf: Optional[float] = None
    final_shot_means: list[float] = field(default_factory=list)
    final_shot_worst_1s: list[float] = field(default_factory=list)
    final_boundary_worst_1s: list[float] = field(default_factory=list)
    refinement_rounds: int = 0
    holdout_refinement_rounds: int = 0
    candidate_encodes: int = 0
    vmaf_executions: int = 0
    measurement_cache_hits: int = 0
    phase_seconds: dict[str, float] = field(default_factory=dict)
    resource_cpu_seconds: Optional[float] = None
    phase_cpu_seconds: dict[str, float] = field(default_factory=dict)
    resource_gpu_seconds: Optional[float] = None

    @property
    def success(self) -> bool:
        return self.status == QualitySearchStatus.FOUND


@dataclass(frozen=True, slots=True)
class DecisionOption:
    action_code: DecisionActionCode
    suggested_value: Optional[float | str] = None
    requires_analysis: bool = False
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class AnalysisReceipt:
    schema_version: int
    measurement_fingerprint: str
    source_identity: dict[str, object]
    ffmpeg_identity: dict[str, object]
    encoder_identity: dict[str, object]
    sample_scheme_version: int
    sample_windows: list[tuple[float, float]]
    scout_windows: list[dict[str, object]] = field(default_factory=list)
    search_windows: list[dict[str, object]] = field(default_factory=list)
    holdout_windows: list[dict[str, object]] = field(default_factory=list)
    reserve_windows: list[dict[str, object]] = field(default_factory=list)
    refinement_rounds: list[dict[str, object]] = field(default_factory=list)
    search_min_vmaf: Optional[float] = None
    holdout_min_vmaf: Optional[float] = None
    search_fingerprint: str = ""
    measurement_configuration: dict[str, object] = field(default_factory=dict)
    candidates: list[QualityCandidateResult] = field(default_factory=list)
    created_at: str = ""
    content_uncertainty: float = 0.0
    content_heterogeneity: float = 0.0
    adaptive_expansion_events: list[dict[str, object]] = field(default_factory=list)
    rd_ambiguity_events: list[dict[str, object]] = field(default_factory=list)
    size_calibration_windows: list[dict[str, object]] = field(default_factory=list)
    viewing_context: str = VmafViewingContext.HIGH_FIDELITY.value
    selected_vmaf_model: str = ""
    independent_final_holdout: bool = False
