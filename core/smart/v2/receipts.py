"""Versioned, isolated v2 measurements. Policy changes do not identify measurements."""

from __future__ import annotations

from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import uuid

from core.models import EncodePlanItem, SegmentedAnalysisResult, ShotAnalysis, ShotCandidate, ShotRange
from core.ffmpeg.segmented import common_hevc_level
from core.smart.cache import measurement_configuration_payload, path_identity
from core.smart.v2.optimizer import SETTINGS


SCHEMA = 1
SCHEME = 1


def fingerprint(ffmpeg: Path, ffprobe: Path, item: EncodePlanItem) -> str:
    payload = {
        "algorithm": "v2_experimental", "schema": SCHEMA, "scheme": SCHEME,
        "measurement": measurement_configuration_payload(ffmpeg, item),
        "ffprobe": path_identity(ffprobe), "profile": asdict(SETTINGS[item.options.analysis_profile]),
        "decode_acceleration": item.options.decode_acceleration.value,
        "hevc_level": common_hevc_level(item) if item.encoder_info and item.encoder_info.encoder_name == "libx265" else "encoder_native",
        "stream_configuration_version": 2,
        "scene": {"threshold": 10, "width": 480, "cadence": "all_frames"},
        "size_margin": 0.10, "quality_aggregation": "duration_mean_and_local_floors_v1",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def receipt_root(workdir: Path, key: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", key) is None:
        raise ValueError("Invalid v2 measurement fingerprint.")
    return workdir / "analysis" / "smart-v2" / key


def delete_receipt(workdir: Path, key: str) -> None:
    (receipt_root(workdir, key) / "receipt.json").unlink(missing_ok=True)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def timeline_identity(shots: list[dict]) -> str:
    coordinates = [{k: shot[k] for k in ("shot", "search_windows", "holdout_window")} for shot in shots]
    return hashlib.sha256(json.dumps(coordinates, sort_keys=True).encode()).hexdigest()


def save(root: Path, result: SegmentedAnalysisResult) -> None:
    shots = []
    for analysis in result.shots:
        entry = asdict(analysis)
        for data, candidate in zip(entry["candidates"], analysis.candidates):
            path = candidate.artifact
            data["artifact"] = None
            data["artifact_hash"] = None
            if path is not None and path.is_file() and path.is_relative_to(root):
                data["artifact"] = str(path.relative_to(root))
                candidate.artifact_hash = file_hash(path)
                data["artifact_hash"] = candidate.artifact_hash
        shots.append(entry)
    payload = {"schema": SCHEMA, "scheme": SCHEME, "fingerprint": result.measurement_fingerprint,
               "timeline_fingerprint": timeline_identity(shots),
               "fps": result.fps, "fps_rational": result.fps_rational,
               "source_start_sec": result.source_start_sec, "source_frames": result.source_frames,
               "shots": shots}
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".receipt-{uuid.uuid4().hex}.json"
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(root / "receipt.json")
    finally:
        temporary.unlink(missing_ok=True)


def load(root: Path, result: SegmentedAnalysisResult) -> bool:
    try:
        data = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
        if (data["schema"] != SCHEMA or data["scheme"] != SCHEME
                or data["fingerprint"] != result.measurement_fingerprint
                or data.get("timeline_fingerprint") != timeline_identity(data["shots"])):
            return False
        fps, origin, frames = float(data["fps"]), float(data["source_start_sec"]), int(data["source_frames"])
        if not math.isfinite(fps) or fps <= 0 or not math.isfinite(origin) or frames <= 0:
            return False
        if not math.isclose(float(Fraction(data["fps_rational"])), fps, rel_tol=1e-9):
            return False
        shots = []
        previous = 0
        for entry in data["shots"]:
            shot = ShotRange(**entry["shot"])
            if (type(shot.start_frame) is not int or type(shot.end_frame) is not int
                    or shot.start_frame != previous or shot.end_frame <= previous or shot.end_frame > frames):
                return False
            previous = shot.end_frame
            windows: list[ShotRange] = [ShotRange(**window) for window in entry["search_windows"]]
            holdout: ShotRange | None = ShotRange(**entry["holdout_window"]) if entry["holdout_window"] else None
            occupied: list[ShotRange] = list(windows)
            if holdout is not None:
                occupied.append(holdout)
            occupied.sort(key=lambda w: w.start_frame)
            if (not windows or any(w.start_frame < shot.start_frame or w.end_frame > shot.end_frame
                                   or w.frame_count <= 0 for w in occupied)
                    or any(a.end_frame > b.start_frame for a, b in zip(occupied, occupied[1:]))):
                return False
            candidates = []
            for raw in entry["candidates"]:
                raw = dict(raw)
                artifact_hash = raw.pop("artifact_hash", None)
                artifact = raw.get("artifact")
                raw["artifact"] = None
                raw["artifact_hash"] = None
                if artifact:
                    path = (root / artifact).resolve()
                    if path.is_relative_to(root.resolve()) and path.is_file() and file_hash(path) == artifact_hash:
                        raw["artifact"] = path
                        raw["artifact_hash"] = artifact_hash
                candidate = ShotCandidate(**raw)
                if (candidate.bitrate_bps <= 0 or candidate.predicted_video_bytes < 0 or candidate.measured_frames <= 0
                        or candidate.measured_frames > shot.frame_count
                        or any(not math.isfinite(q) or not 0 <= q <= 100
                               for q in (candidate.mean_vmaf, candidate.worst_1s_vmaf))):
                    return False
                candidates.append(candidate)
            shots.append(ShotAnalysis(shot, windows, holdout, candidates))
        if previous != frames:
            return False
        result.shots = shots
        result.fps, result.fps_rational = fps, str(data["fps_rational"])
        result.source_start_sec, result.source_frames = origin, frames
        result.measurement_cache_hits = sum(len(s.candidates) for s in shots)
        return True
    except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError):
        return False
