from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from core.models import AnalysisReceipt, QualityCandidateResult, SizePrediction


ANALYSIS_RECEIPT_SCHEMA_VERSION = 5
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_RECEIPT_LOCK = threading.RLock()
_WINDOW_EPSILON_SEC = 1e-9


def analysis_receipts_dir(workdir: Path) -> Path:
    return workdir / "analysis" / "receipts"


def analysis_receipt_path(workdir: Path, measurement_fingerprint: str) -> Path:
    if _FINGERPRINT_RE.fullmatch(measurement_fingerprint) is None:
        raise ValueError("Analysis receipt fingerprint must be a SHA-256 hex digest.")
    return analysis_receipts_dir(workdir) / f"{measurement_fingerprint}.json"


def _candidate_from_data(data: object) -> QualityCandidateResult:
    if not isinstance(data, dict):
        raise ValueError("Analysis receipt candidate must be an object.")
    video_bitrate_bps = int(data["video_bitrate_bps"])
    segment_vmaf = [float(value) for value in data.get("segment_vmaf", [])]
    min_vmaf = float(data.get("min_vmaf", 0.0))
    encoded_bytes = [int(value) for value in data.get("encoded_bytes", [])]
    encoded_durations_sec = [float(value) for value in data.get("encoded_durations_sec", [])]
    observed_video_bitrate_bps = int(data.get("observed_video_bitrate_bps", 0))
    predicted_output_bytes = (
        None if data.get("predicted_output_bytes") is None else int(data["predicted_output_bytes"])
    )
    predicted_output_ratio = (
        None if data.get("predicted_output_ratio") is None else float(data["predicted_output_ratio"])
    )
    segment_mean_vmaf = [float(value) for value in data.get("segment_mean_vmaf", [])]
    segment_p10_vmaf = [float(value) for value in data.get("segment_p10_vmaf", [])]
    segment_worst_1s_vmaf = [float(value) for value in data.get("segment_worst_1s_vmaf", [])]
    segment_quality_scores = [float(value) for value in data.get("segment_quality_scores", segment_vmaf)]
    observed_window_bitrates = [int(value) for value in data.get("observed_window_bitrates", [])]
    raw_prediction = data.get("size_prediction")
    size_prediction = None
    if isinstance(raw_prediction, dict):
        size_prediction = SizePrediction(
            mean_video_bitrate_bps=int(raw_prediction["mean_video_bitrate_bps"]),
            upper_video_bitrate_bps=int(raw_prediction["upper_video_bitrate_bps"]),
            predicted_output_bytes=int(raw_prediction["predicted_output_bytes"]),
            predicted_output_ratio=(None if raw_prediction.get("predicted_output_ratio") is None else float(raw_prediction["predicted_output_ratio"])),
            uncertainty=float(raw_prediction["uncertainty"]),
            method=str(raw_prediction["method"]),
        )
    if video_bitrate_bps <= 0 or observed_video_bitrate_bps < 0:
        raise ValueError("Analysis receipt contains an invalid bitrate.")
    if not math.isfinite(min_vmaf) or any(not math.isfinite(value) for value in segment_vmaf):
        raise ValueError("Analysis receipt contains a non-finite VMAF value.")
    if not 0.0 <= min_vmaf <= 100.0 or any(
        not 0.0 <= value <= 100.0 for value in segment_vmaf
    ):
        raise ValueError("Analysis receipt contains an out-of-range VMAF value.")
    if any(value < 0 for value in encoded_bytes):
        raise ValueError("Analysis receipt contains an invalid encoded size.")
    if any(not math.isfinite(value) or value <= 0 for value in encoded_durations_sec):
        raise ValueError("Analysis receipt contains an invalid encoded duration.")
    if len(encoded_bytes) != len(encoded_durations_sec):
        raise ValueError("Analysis receipt sample measurements have different lengths.")
    if predicted_output_bytes is not None and predicted_output_bytes < 0:
        raise ValueError("Analysis receipt contains an invalid output size prediction.")
    if predicted_output_ratio is not None and (
        not math.isfinite(predicted_output_ratio) or predicted_output_ratio < 0
    ):
        raise ValueError("Analysis receipt contains an invalid output ratio prediction.")
    return QualityCandidateResult(
        video_bitrate_bps=video_bitrate_bps,
        segment_vmaf=segment_vmaf,
        min_vmaf=min_vmaf,
        encoded_bytes=encoded_bytes,
        encoded_durations_sec=encoded_durations_sec,
        observed_video_bitrate_bps=observed_video_bitrate_bps,
        predicted_output_bytes=predicted_output_bytes,
        predicted_output_ratio=predicted_output_ratio,
        segment_mean_vmaf=segment_mean_vmaf,
        segment_p10_vmaf=segment_p10_vmaf,
        segment_worst_1s_vmaf=segment_worst_1s_vmaf,
        segment_quality_scores=segment_quality_scores,
        observed_window_bitrates=observed_window_bitrates,
        size_prediction=size_prediction,
        rd_ambiguous=bool(data.get("rd_ambiguous", False)),
    )


def _identity(data: object, field_name: str) -> dict[str, object]:
    if not isinstance(data, dict):
        raise ValueError(f"Analysis receipt {field_name} must be an object.")
    return {str(key): value for key, value in data.items()}


def _object_list(data: object, field_name: str) -> list[dict[str, object]]:
    if not isinstance(data, list):
        raise ValueError(f"Analysis receipt {field_name} must be a list.")
    result: list[dict[str, object]] = []
    for value in data:
        if not isinstance(value, dict):
            raise ValueError(f"Analysis receipt {field_name} entries must be objects.")
        normalized = {str(key): item for key, item in value.items()}
        _validate_json_value(normalized, field_name)
        result.append(normalized)
    return result


def _validate_json_value(value: object, field_name: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Analysis receipt {field_name} contains a non-finite value.")
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, field_name)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_json_value(item, field_name)


def _planned_windows(data: object, field_name: str) -> list[dict[str, object]]:
    windows = _object_list(data, field_name)
    for window in windows:
        try:
            start = float(str(window["start_sec"]))
            duration = float(str(window["duration_sec"]))
            identifier = str(window["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Analysis receipt {field_name} entry is invalid.") from exc
        if not identifier or not math.isfinite(start) or start < 0 or not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"Analysis receipt {field_name} entry is invalid.")
    return windows


def _scout_windows(data: object) -> list[dict[str, object]]:
    windows = _planned_windows(data, "scout_windows")
    for window in windows:
        try:
            metrics = (float(str(window["si_p90"])), float(str(window["ti_p90"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Analysis receipt scout window metrics are invalid.") from exc
        if any(not math.isfinite(value) for value in metrics):
            raise ValueError("Analysis receipt scout window metrics are invalid.")
    return windows


def _optional_score(data: object, field_name: str) -> float | None:
    if data is None:
        return None
    value = float(str(data))
    if not math.isfinite(value) or not 0.0 <= value <= 100.0:
        raise ValueError(f"Analysis receipt {field_name} is invalid.")
    return value


def _window_coordinates(windows: list[dict[str, object]]) -> list[tuple[float, float]]:
    return [
        (float(str(window["start_sec"])), float(str(window["duration_sec"])))
        for window in windows
    ]


def _windows_overlap(left: tuple[float, float], right: tuple[float, float]) -> bool:
    left_start, left_duration = left
    right_start, right_duration = right
    return (
        left_start < right_start + right_duration - _WINDOW_EPSILON_SEC
        and right_start < left_start + left_duration - _WINDOW_EPSILON_SEC
    )


def _receipt_from_data(data: object) -> AnalysisReceipt:
    if not isinstance(data, dict):
        raise ValueError("Analysis receipt must be an object.")
    schema_version = int(data.get("schema_version", 0))
    if schema_version != ANALYSIS_RECEIPT_SCHEMA_VERSION:
        raise ValueError(f"Unsupported analysis receipt schema: {schema_version}")
    fingerprint = str(data["measurement_fingerprint"])
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("Analysis receipt contains an invalid fingerprint.")
    raw_windows = data.get("sample_windows", [])
    if not isinstance(raw_windows, list):
        raise ValueError("Analysis receipt sample_windows must be a list.")
    windows: list[tuple[float, float]] = []
    for window in raw_windows:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            raise ValueError("Analysis receipt sample window must contain start and duration.")
        start_sec, duration_sec = float(window[0]), float(window[1])
        if not math.isfinite(start_sec) or start_sec < 0 or not math.isfinite(duration_sec) or duration_sec <= 0:
            raise ValueError("Analysis receipt contains an invalid sample window.")
        windows.append((start_sec, duration_sec))
    raw_candidates = data.get("candidates", [])
    if not isinstance(raw_candidates, list):
        raise ValueError("Analysis receipt candidates must be a list.")
    candidates = [_candidate_from_data(candidate) for candidate in raw_candidates]
    for candidate in candidates:
        if len(candidate.segment_vmaf) != len(windows):
            raise ValueError("Analysis receipt candidate does not cover every sample window.")
        if candidate.segment_vmaf and not math.isclose(
            candidate.min_vmaf,
            min(candidate.segment_vmaf),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("Analysis receipt candidate VMAF summary is inconsistent.")
    search_fingerprint = str(data.get("search_fingerprint", ""))
    if search_fingerprint and _FINGERPRINT_RE.fullmatch(search_fingerprint) is None:
        raise ValueError("Analysis receipt contains an invalid search fingerprint.")
    search_windows = _planned_windows(data.get("search_windows", []), "search_windows")
    holdout_windows = _planned_windows(data.get("holdout_windows", []), "holdout_windows")
    reserve_windows = _planned_windows(data.get("reserve_windows", []), "reserve_windows")
    search_coordinates = _window_coordinates(search_windows)
    holdout_coordinates = _window_coordinates(holdout_windows)
    reserve_coordinates = _window_coordinates(reserve_windows)
    if windows != search_coordinates:
        raise ValueError("Analysis receipt sample and search windows are inconsistent.")
    if any(
        _windows_overlap(search, holdout)
        for search in search_coordinates
        for holdout in holdout_coordinates
    ):
        raise ValueError("Analysis receipt search and holdout windows overlap.")
    all_pools = (search_coordinates, holdout_coordinates, reserve_coordinates)
    for left_index, left_pool in enumerate(all_pools):
        for right_pool in all_pools[left_index + 1 :]:
            if any(_windows_overlap(left, right) for left in left_pool for right in right_pool):
                raise ValueError("Analysis receipt sample pools overlap.")
    measurement_configuration = _identity(
        data.get("measurement_configuration"), "measurement_configuration"
    )
    return AnalysisReceipt(
        schema_version=schema_version,
        measurement_fingerprint=fingerprint,
        source_identity=_identity(data.get("source_identity"), "source_identity"),
        ffmpeg_identity=_identity(data.get("ffmpeg_identity"), "ffmpeg_identity"),
        encoder_identity=_identity(data.get("encoder_identity"), "encoder_identity"),
        sample_scheme_version=int(data.get("sample_scheme_version", 0)),
        sample_windows=windows,
        scout_windows=_scout_windows(data.get("scout_windows", [])),
        search_windows=search_windows,
        holdout_windows=holdout_windows,
        reserve_windows=reserve_windows,
        refinement_rounds=_object_list(data.get("refinement_rounds", []), "refinement_rounds"),
        search_min_vmaf=_optional_score(data.get("search_min_vmaf"), "search_min_vmaf"),
        holdout_min_vmaf=_optional_score(data.get("holdout_min_vmaf"), "holdout_min_vmaf"),
        search_fingerprint=search_fingerprint,
        measurement_configuration=measurement_configuration,
        candidates=candidates,
        created_at=str(data.get("created_at", "")),
        content_uncertainty=float(data.get("content_uncertainty", 0.0)),
        content_heterogeneity=float(data.get("content_heterogeneity", 0.0)),
        adaptive_expansion_events=_object_list(data.get("adaptive_expansion_events", []), "adaptive_expansion_events"),
        rd_ambiguity_events=_object_list(data.get("rd_ambiguity_events", []), "rd_ambiguity_events"),
        size_calibration_windows=_object_list(data.get("size_calibration_windows", []), "size_calibration_windows"),
        viewing_context=str(data.get("viewing_context", "high_fidelity")),
        selected_vmaf_model=str(data.get("selected_vmaf_model", "")),
        independent_final_holdout=bool(data.get("independent_final_holdout", False)),
    )


def load_analysis_receipt(workdir: Path, measurement_fingerprint: str) -> AnalysisReceipt | None:
    path = analysis_receipt_path(workdir, measurement_fingerprint)
    with _RECEIPT_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            receipt = _receipt_from_data(data)
        except (FileNotFoundError, OSError, KeyError, OverflowError, TypeError, ValueError, json.JSONDecodeError):
            return None
    if receipt.measurement_fingerprint != measurement_fingerprint:
        return None
    return receipt


def save_analysis_receipt(workdir: Path, receipt: AnalysisReceipt) -> Path:
    path = analysis_receipt_path(workdir, receipt.measurement_fingerprint)
    payload: dict[str, Any] = asdict(receipt)
    _receipt_from_data(payload)
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    with _RECEIPT_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write(serialized)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def delete_analysis_receipt(workdir: Path, measurement_fingerprint: str) -> None:
    path = analysis_receipt_path(workdir, measurement_fingerprint)
    with _RECEIPT_LOCK:
        path.unlink(missing_ok=True)


def clear_analysis_receipts(workdir: Path) -> int:
    root = analysis_receipts_dir(workdir)
    removed = 0
    with _RECEIPT_LOCK:
        if not root.exists():
            return 0
        for path in root.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed
