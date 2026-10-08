"""Run the optional, manifest-driven SMART product evaluation harness.

Each corpus case either embeds a measurement (useful for importing historical
runs) or supplies an argv-style runner command. Commands are executed without a
shell and must write one measurement JSON object to ``{result_path}``. Available
command placeholders are ``source``, ``ffmpeg``, ``ffprobe``, ``case_id``,
``case_dir``, and ``result_path``.

Minimal manifest::

    {
      "schema_version": 1,
      "cases": [{
        "id": "motion-01",
        "source": "corpus/motion-01.mkv",
        "command": ["runner", "--source", "{source}",
                    "--ffmpeg", "{ffmpeg}", "--ffprobe", "{ffprobe}",
                    "--result", "{result_path}"]
      }]
    }

The result object contains timings, SMART/ground-truth decisions, selected and
oracle bitrates, size observations, and the five counters defined by SMART v2.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.smart.v1.evaluation import (  # noqa: E402
    EVALUATION_RECORD_SCHEMA_VERSION,
    EvaluationCase,
    EvaluationDataError,
    aggregate_evaluation_records,
    calculate_case_metrics,
    load_evaluation_manifest,
    metrics_payload,
)


COUNT_NAMES = (
    "scout_windows",
    "quality_encodes",
    "vmaf_measurements",
    "holdout_measurements",
    "size_calibration_encodes",
)
CSV_FIELDS = (
    "case_id",
    "source",
    "error",
    "analysis_cost",
    "quality_false_pass",
    "false_size_block",
    "bitrate_regret_absolute_bps",
    "bitrate_regret_normalized",
    *COUNT_NAMES,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate SMART accuracy, size decisions, regret, and analysis cost on a corpus."
    )
    parser.add_argument("manifest", type=Path, help="Corpus manifest JSON")
    parser.add_argument("--ffmpeg", type=Path, required=True, help="Exact ffmpeg executable used by every case")
    parser.add_argument("--ffprobe", type=Path, required=True, help="Exact ffprobe executable used by every case")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: workdir/smart-evaluation/<UTC timestamp>)",
    )
    parser.add_argument("--resume", action="store_true", help="Reuse completed case records in results.ndjson")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first failed case")
    return parser


def _require_tool(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise EvaluationDataError(f"{label} is not a file: {resolved}")
    return resolved


def _default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPOSITORY_ROOT / "workdir" / "smart-evaluation" / stamp


def _read_measurement(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationDataError(f"Runner did not produce valid JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise EvaluationDataError(f"Runner result must be a JSON object: {path}")
    return data


def _render_command(
    case: EvaluationCase,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    case_dir: Path,
    result_path: Path,
) -> list[str]:
    replacements = {
        "source": str(case.source_path),
        "ffmpeg": str(ffmpeg),
        "ffprobe": str(ffprobe),
        "case_id": case.id,
        "case_dir": str(case_dir),
        "result_path": str(result_path),
    }
    rendered: list[str] = []
    for part in case.command:
        try:
            rendered.append(part.format_map(replacements))
        except KeyError as exc:
            raise EvaluationDataError(
                f"Case {case.id} command uses unknown placeholder {exc.args[0]!r}"
            ) from exc
    return rendered


def _counts(measurement: Mapping[str, object]) -> dict[str, int]:
    raw = measurement.get("counts", {})
    if not isinstance(raw, dict):
        raise EvaluationDataError("counts must be an object")
    result: dict[str, int] = {}
    for name in COUNT_NAMES:
        value = raw.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise EvaluationDataError(f"counts.{name} must be a non-negative integer")
        result[name] = value
    return result


def _run_case(case: EvaluationCase, ffmpeg: Path, ffprobe: Path, output_dir: Path) -> dict[str, object]:
    case_dir = output_dir / "cases" / case.id
    case_dir.mkdir(parents=True, exist_ok=True)
    if not case.source_path.is_file():
        raise EvaluationDataError(f"Case {case.id} source is not a file: {case.source_path}")
    if case.measurement is not None:
        measurement = dict(case.measurement)
    else:
        result_path = case_dir / "measurement.json"
        command = _render_command(
            case,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            case_dir=case_dir,
            result_path=result_path,
        )
        completed = subprocess.run(command, check=False, cwd=REPOSITORY_ROOT)
        if completed.returncode != 0:
            raise RuntimeError(f"Case {case.id} runner exited with {completed.returncode}")
        measurement = _read_measurement(result_path)
    metrics = calculate_case_metrics(measurement)
    return {
        "schema_version": EVALUATION_RECORD_SCHEMA_VERSION,
        "case_id": case.id,
        "source": str(case.source_path),
        "metadata": case.metadata,
        "measurement": measurement,
        "metrics": metrics_payload(metrics),
        "counts": _counts(measurement),
        "error": None,
    }


def _load_existing(path: Path) -> dict[str, dict[str, object]]:
    if not path.is_file():
        return {}
    records: dict[str, dict[str, object]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvaluationDataError(f"Invalid resume record at line {line_number}: {exc}") from exc
        if not isinstance(record, dict) or not isinstance(record.get("case_id"), str):
            raise EvaluationDataError(f"Invalid resume record at line {line_number}")
        records[str(record["case_id"])] = record
    return records


def _atomic_json(path: Path, data: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _write_csv(path: Path, records: list[dict[str, object]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            raw_metrics = record.get("metrics")
            metrics: dict[str, object] = raw_metrics if isinstance(raw_metrics, dict) else {}
            raw_counts = record.get("counts")
            counts: dict[str, object] = raw_counts if isinstance(raw_counts, dict) else {}
            writer.writerow(
                {
                    "case_id": record.get("case_id"),
                    "source": record.get("source"),
                    "error": record.get("error"),
                    **{name: metrics.get(name) for name in CSV_FIELDS if name in metrics},
                    **{name: counts.get(name) for name in COUNT_NAMES},
                }
            )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ffmpeg = _require_tool(args.ffmpeg, "ffmpeg")
        ffprobe = _require_tool(args.ffprobe, "ffprobe")
        cases = load_evaluation_manifest(args.manifest.expanduser().resolve())
        output_dir = (args.output_dir or _default_output_dir()).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        ndjson_path = output_dir / "results.ndjson"
        existing = _load_existing(ndjson_path) if args.resume else {}
        records: list[dict[str, object]] = []
        with ndjson_path.open("a" if args.resume else "w", encoding="utf-8") as stream:
            for case in cases:
                if case.id in existing and existing[case.id].get("error") is None:
                    records.append(existing[case.id])
                    continue
                try:
                    record = _run_case(case, ffmpeg, ffprobe, output_dir)
                except Exception as exc:
                    record = {
                        "schema_version": EVALUATION_RECORD_SCHEMA_VERSION,
                        "case_id": case.id,
                        "source": str(case.source_path),
                        "metadata": case.metadata,
                        "metrics": {},
                        "counts": {},
                        "error": str(exc),
                    }
                records.append(record)
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                if record["error"] is not None and args.fail_fast:
                    break
        summary = aggregate_evaluation_records(records)
        summary.update(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "manifest": str(args.manifest.expanduser().resolve()),
                "ffmpeg": str(ffmpeg),
                "ffprobe": str(ffprobe),
            }
        )
        _atomic_json(output_dir / "summary.json", summary)
        _write_csv(output_dir / "results.csv", records)
        print(output_dir)
        return 1 if any(record.get("error") is not None for record in records) else 0
    except (EvaluationDataError, OSError, ValueError) as exc:
        print(f"SMART evaluation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
