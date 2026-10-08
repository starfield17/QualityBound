"""Pure data contracts and metrics for optional SMART corpus evaluation."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping, Sequence


EVALUATION_MANIFEST_SCHEMA_VERSION = 1
EVALUATION_RECORD_SCHEMA_VERSION = 1


class EvaluationDataError(ValueError):
    """An evaluation manifest or measurement violates the public contract."""


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    id: str
    source_path: Path
    command: tuple[str, ...] = ()
    measurement: dict[str, object] | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BitrateRegret:
    absolute_bps: int
    normalized: float


@dataclass(frozen=True, slots=True)
class CaseMetrics:
    analysis_cost: float | None
    quality_false_pass: bool | None
    false_size_block: bool | None
    bitrate_regret_absolute_bps: int | None
    bitrate_regret_normalized: float | None


def _finite_nonnegative(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EvaluationDataError(f"{name} must be a finite non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationDataError(f"{name} must be a finite non-negative number") from exc
    if not math.isfinite(number) or number < 0:
        raise EvaluationDataError(f"{name} must be a finite non-negative number")
    return number


def _optional_bool(data: Mapping[str, object], name: str) -> bool | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise EvaluationDataError(f"{name} must be a boolean or null")
    return value


def _optional_positive_int(data: Mapping[str, object], name: str) -> int | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise EvaluationDataError(f"{name} must be a positive integer or null")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise EvaluationDataError(f"{name} must be a positive integer or null") from exc
    numeric_value = float(value)
    if number <= 0 or number != numeric_value:
        raise EvaluationDataError(f"{name} must be a positive integer or null")
    return number


def analysis_cost(
    analysis_wall_seconds: float,
    final_encode_wall_seconds: float,
) -> float:
    """Return SMART analysis wall time divided by final encode wall time."""

    analysis = _finite_nonnegative(analysis_wall_seconds, "analysis_wall_seconds")
    final_encode = _finite_nonnegative(final_encode_wall_seconds, "final_encode_wall_seconds")
    if final_encode <= 0:
        raise EvaluationDataError("final_encode_wall_seconds must be greater than zero")
    return analysis / final_encode


def is_quality_false_pass(*, smart_passed: bool, ground_truth_passed: bool) -> bool:
    """Return whether SMART passed a case rejected by dense/full validation."""

    return bool(smart_passed and not ground_truth_passed)


def is_false_size_block(
    *,
    smart_size_blocked: bool,
    full_encode_output_bytes: int,
    max_output_bytes: int,
) -> bool:
    """Return whether a supposedly impossible full encode actually fits."""

    actual = _optional_positive_int(
        {"full_encode_output_bytes": full_encode_output_bytes},
        "full_encode_output_bytes",
    )
    maximum = _optional_positive_int(
        {"max_output_bytes": max_output_bytes}, "max_output_bytes"
    )
    assert actual is not None and maximum is not None
    return bool(smart_size_blocked and actual <= maximum)


def bitrate_regret(
    selected_video_bitrate_bps: int,
    oracle_minimum_bitrate_bps: int,
) -> BitrateRegret:
    """Return absolute and oracle-normalized bitrate regret."""

    selected = _optional_positive_int(
        {"selected_video_bitrate_bps": selected_video_bitrate_bps},
        "selected_video_bitrate_bps",
    )
    oracle = _optional_positive_int(
        {"oracle_minimum_bitrate_bps": oracle_minimum_bitrate_bps},
        "oracle_minimum_bitrate_bps",
    )
    assert selected is not None and oracle is not None
    absolute = selected - oracle
    return BitrateRegret(absolute_bps=absolute, normalized=absolute / oracle)


def calculate_case_metrics(measurement: Mapping[str, object]) -> CaseMetrics:
    """Calculate all metrics available in one runner measurement payload.

    Missing ground-truth or oracle fields intentionally produce ``None``. This
    lets a failed or size-blocked case remain auditable without inventing a
    denominator for a metric that could not be observed.
    """

    analysis_seconds = _finite_nonnegative(
        measurement.get("analysis_wall_seconds"), "analysis_wall_seconds"
    )
    final_value = measurement.get("final_encode_wall_seconds")
    cost = None
    if final_value is not None:
        final_seconds = _finite_nonnegative(
            final_value, "final_encode_wall_seconds"
        )
        cost = analysis_cost(analysis_seconds, final_seconds)

    smart_passed = _optional_bool(measurement, "smart_passed")
    if smart_passed is None:
        raise EvaluationDataError("smart_passed is required")
    ground_truth_passed = _optional_bool(measurement, "ground_truth_passed")
    false_pass = (
        None
        if not smart_passed or ground_truth_passed is None
        else is_quality_false_pass(
            smart_passed=smart_passed,
            ground_truth_passed=ground_truth_passed,
        )
    )

    smart_size_blocked = _optional_bool(measurement, "smart_size_blocked")
    if smart_size_blocked is None:
        raise EvaluationDataError("smart_size_blocked is required")
    actual_bytes = _optional_positive_int(measurement, "full_encode_output_bytes")
    max_bytes = _optional_positive_int(measurement, "max_output_bytes")
    size_block = (
        None
        if not smart_size_blocked or actual_bytes is None or max_bytes is None
        else is_false_size_block(
            smart_size_blocked=smart_size_blocked,
            full_encode_output_bytes=actual_bytes,
            max_output_bytes=max_bytes,
        )
    )

    selected = _optional_positive_int(measurement, "selected_video_bitrate_bps")
    oracle = _optional_positive_int(measurement, "oracle_minimum_bitrate_bps")
    regret = None if selected is None or oracle is None else bitrate_regret(selected, oracle)
    return CaseMetrics(
        analysis_cost=cost,
        quality_false_pass=false_pass,
        false_size_block=size_block,
        bitrate_regret_absolute_bps=None if regret is None else regret.absolute_bps,
        bitrate_regret_normalized=None if regret is None else regret.normalized,
    )


def load_evaluation_manifest(path: Path) -> tuple[EvaluationCase, ...]:
    """Load a corpus manifest and resolve source paths relative to that file."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDataError(f"Could not load evaluation manifest {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != EVALUATION_MANIFEST_SCHEMA_VERSION:
        raise EvaluationDataError(
            f"Evaluation manifest schema_version must be {EVALUATION_MANIFEST_SCHEMA_VERSION}"
        )
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationDataError("Evaluation manifest must contain a non-empty cases list")

    cases: list[EvaluationCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise EvaluationDataError(f"cases[{index}] must be an object")
        case_id = str(raw.get("id", "")).strip()
        if not case_id or case_id in seen or any(char in case_id for char in "/\\"):
            raise EvaluationDataError(f"cases[{index}].id must be unique and path-safe")
        seen.add(case_id)
        source = raw.get("source")
        if not isinstance(source, str) or not source.strip():
            raise EvaluationDataError(f"cases[{index}].source must be a path string")
        command_raw = raw.get("command", [])
        if not isinstance(command_raw, list) or any(
            not isinstance(part, str) or not part for part in command_raw
        ):
            raise EvaluationDataError(f"cases[{index}].command must be a string list")
        measurement_raw = raw.get("measurement")
        if measurement_raw is not None and not isinstance(measurement_raw, dict):
            raise EvaluationDataError(f"cases[{index}].measurement must be an object")
        if bool(command_raw) == (measurement_raw is not None):
            raise EvaluationDataError(
                f"cases[{index}] must provide exactly one of command or measurement"
            )
        metadata_raw = raw.get("metadata", {})
        if not isinstance(metadata_raw, dict):
            raise EvaluationDataError(f"cases[{index}].metadata must be an object")
        source_path = Path(source).expanduser()
        if not source_path.is_absolute():
            source_path = path.parent / source_path
        cases.append(
            EvaluationCase(
                id=case_id,
                source_path=source_path.resolve(),
                command=tuple(command_raw),
                measurement=None if measurement_raw is None else dict(measurement_raw),
                metadata=dict(metadata_raw),
            )
        )
    return tuple(cases)


def aggregate_evaluation_records(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Aggregate successful case records with explicit metric denominators."""

    metric_rows = [row.get("metrics") for row in records if row.get("error") is None]
    metrics = [row for row in metric_rows if isinstance(row, dict)]

    def values(name: str) -> list[float]:
        return [float(row[name]) for row in metrics if row.get(name) is not None]

    def boolean_rate(name: str) -> tuple[int, int, float | None]:
        observed = [bool(row[name]) for row in metrics if row.get(name) is not None]
        count = sum(observed)
        return count, len(observed), None if not observed else count / len(observed)

    costs = values("analysis_cost")
    regrets_bps = values("bitrate_regret_absolute_bps")
    regrets_normalized = values("bitrate_regret_normalized")
    false_passes, false_pass_denominator, false_pass_rate = boolean_rate("quality_false_pass")
    false_blocks, false_block_denominator, false_block_rate = boolean_rate("false_size_block")
    count_names = (
        "scout_windows",
        "quality_encodes",
        "vmaf_measurements",
        "holdout_measurements",
        "size_calibration_encodes",
    )
    totals = {name: 0 for name in count_names}
    for record in records:
        counts = record.get("counts")
        if not isinstance(counts, dict):
            continue
        for name in count_names:
            value = counts.get(name, 0)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[name] += value
    return {
        "schema_version": EVALUATION_RECORD_SCHEMA_VERSION,
        "case_count": len(records),
        "successful_case_count": sum(record.get("error") is None for record in records),
        "failed_case_count": sum(record.get("error") is not None for record in records),
        "mean_analysis_cost": None if not costs else sum(costs) / len(costs),
        "analysis_cost_denominator": len(costs),
        "quality_false_pass_count": false_passes,
        "quality_false_pass_denominator": false_pass_denominator,
        "quality_false_pass_rate": false_pass_rate,
        "false_size_block_count": false_blocks,
        "false_size_block_denominator": false_block_denominator,
        "false_size_block_rate": false_block_rate,
        "mean_bitrate_regret_bps": None if not regrets_bps else sum(regrets_bps) / len(regrets_bps),
        "mean_bitrate_regret_normalized": (
            None if not regrets_normalized else sum(regrets_normalized) / len(regrets_normalized)
        ),
        "bitrate_regret_denominator": len(regrets_bps),
        "totals": totals,
    }


def metrics_payload(metrics: CaseMetrics) -> dict[str, object]:
    return asdict(metrics)
