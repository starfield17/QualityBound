"""Ratchet for the strict-mode inference debt recorded in docs/development.md.

`pyproject.toml` disables six inference-precision rules because capability
snapshots, presets and analysis receipts cross the FFmpeg/config/receipt
boundaries as `dict[str, object]` and JSON-decoded values. The size of that
residue was previously only a number in a document, so it drifted unobserved.

This script measures it through the committed `pyright.strict-debt.json`, which
extends the project config and turns those six rules into warnings, then fails
when any count grows above the recorded baseline. Shrinking is always allowed;
record the new numbers here and in `docs/development.md` when it happens.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
PROBE_CONFIG = ROOT / "pyright.strict-debt.json"
RULES = (
    "reportMissingTypeArgument",
    "reportUnknownArgumentType",
    "reportUnknownLambdaType",
    "reportUnknownMemberType",
    "reportUnknownParameterType",
    "reportUnknownVariableType",
)

# Measured with this script on the commit that introduced it. A count above any
# of these fails; a count below is an improvement and should be re-recorded.
BASELINE: dict[str, int] = {
    "total": 610,
    "core": 483,
    "gui": 116,
    "cli": 11,
    "main.py": 0,
}


def pyright_settings() -> dict[str, Any]:
    """Return the `[tool.pyright]` table from the project's pyproject.toml."""

    with (ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    settings = data.get("tool", {}).get("pyright")
    if not isinstance(settings, dict):
        raise RuntimeError("pyproject.toml has no [tool.pyright] table")
    return settings


def probe_rules() -> dict[str, Any]:
    """Return the rule overrides the committed strict-debt config applies."""

    with PROBE_CONFIG.open("rb") as handle:
        config: dict[str, Any] = json.load(handle)
    return config


def package_counts(files: list[str]) -> dict[str, int]:
    """Count diagnostics per top-level package, matching the baseline keys."""

    counts: Counter[str] = Counter()
    for name in files:
        relative = Path(name).resolve().relative_to(ROOT)
        counts[relative.parts[0]] += 1
    return dict(counts)


def measure(report: dict[str, Any]) -> dict[str, int]:
    """Turn a pyright JSON report into the baseline's comparison keys."""

    files = [
        str(diagnostic["file"])
        for diagnostic in report["generalDiagnostics"]
        if diagnostic["severity"] == "warning"
    ]
    measured = package_counts(files)
    measured["total"] = len(files)
    return {key: measured.get(key, 0) for key in BASELINE}


def regressions(measured: dict[str, int]) -> list[str]:
    """Return the baseline keys that grew, as human-readable lines."""

    return [
        f"{key}: {measured[key]} exceeds the recorded baseline of {BASELINE[key]}"
        for key in BASELINE
        if measured[key] > BASELINE[key]
    ]


def run_pyright() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "pyright", "-p", str(PROBE_CONFIG), "--outputjson"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if not completed.stdout.strip():
        print(completed.stderr, file=sys.stderr)
        raise RuntimeError("pyright produced no JSON report")
    report: dict[str, Any] = json.loads(completed.stdout)
    if report["summary"]["errorCount"]:
        raise RuntimeError(
            "the strict-debt probe reported errors, so its warnings are not "
            "comparable to the baseline: run `pyright` first"
        )
    return report


def main() -> int:
    settings = pyright_settings()
    missing = [rule for rule in RULES if settings.get(rule) != "none"]
    if missing:
        print(
            "These rules are no longer disabled in pyproject.toml, so they need "
            f"a new baseline or removal from this script: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1
    overrides = probe_rules()
    drifted = [rule for rule in RULES if overrides.get(rule) != "warning"]
    if drifted:
        print(
            "pyright.strict-debt.json no longer raises these disabled rules to "
            f"warnings, so the measurement is not the documented residue: {', '.join(drifted)}",
            file=sys.stderr,
        )
        return 1
    measured = measure(run_pyright())
    for key in BASELINE:
        print(f"{key}: {measured[key]} (baseline {BASELINE[key]})")
    failures = regressions(measured)
    if failures:
        print("\nStrict-mode inference debt grew:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nName the boundary (a TypedDict or dataclass per receipt kind, "
            "capability snapshot and preset document) or re-record the measured "
            "baseline in this script and docs/development.md.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
