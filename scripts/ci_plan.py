"""Deterministic CI routing policy for the QualityBound repository.

This module is the single source of truth for the automatic minimum
verification plan that GitHub Actions runs for a given set of changed paths.
It intentionally uses only the Python standard library so that the routing
policy can be unit tested and reproduced byte-for-byte on any runner.

Design rules (see docs/ci-workflows.md):

* The planner is a pure function of the changed paths: Plan(A + B) must equal
  the union of Plan(A) and Plan(B), so no low-risk file may downgrade a
  high-risk one (monotonicity).
* Unknown paths fail closed: a path the router does not recognise requests
  full verification rather than degrading to a quality-only run.
* Ordinary application/runtime changes run Quality plus the three native OS
  unit-test environments but no longer trigger all five Nuitka packaging jobs.
* Packaging/build-system changes trigger the full targeted or all-platform
  native packaging smoke.

The matrices defined here (TEST_TARGETS and PACKAGE_TARGETS) are the single
source of truth for runners and native architectures. GitHub workflow contract
tests read them, so a target change only needs to happen in this file.
"""

from __future__ import annotations

import json
import os
import re
import sys

# --- Canonical matrices (single source of truth) ---------------------------

#: Native OS unit-test environments.
TEST_TARGETS: dict[str, dict[str, str]] = {
    "linux": {
        "platform": "linux",
        "runner": "ubuntu-22.04",
        "architecture": "x64",
    },
    "windows": {
        "platform": "windows",
        "runner": "windows-2022",
        "architecture": "x64",
    },
    "macos": {
        "platform": "macos",
        "runner": "macos-15",
        "architecture": "arm64",
    },
}

#: Native packaging targets. The "target" key is the canonical Nuitka / FFmpeg
#: target name used everywhere in release artifacts.
PACKAGE_TARGETS: dict[str, dict[str, str]] = {
    "windows-x86_64": {
        "target": "windows-x86_64",
        "runner": "windows-2022",
        "architecture": "x64",
        "package_kind": "standalone",
        "windows_compiler": "msvc",
        "installer_architecture": "x86_64",
    },
    "windows-arm64": {
        "target": "windows-arm64",
        "runner": "windows-11-arm",
        "architecture": "arm64",
        "package_kind": "standalone",
        "windows_compiler": "clang",
        "installer_architecture": "arm64",
    },
    "linux-x86_64": {
        "target": "linux-x86_64",
        "runner": "ubuntu-22.04",
        "architecture": "x64",
        "package_kind": "standalone",
    },
    "linux-arm64": {
        "target": "linux-arm64",
        "runner": "ubuntu-24.04-arm",
        "architecture": "arm64",
        "package_kind": "standalone",
    },
    "macos-arm64": {
        "target": "macos-arm64",
        "runner": "macos-15",
        "architecture": "arm64",
        "target_arch": "arm64",
        "package_kind": "macos-app",
    },
}

# Default all-platform packaging mode for automatic CI routing.
DEFAULT_PACKAGE_MODE = "smoke"


# --- Capability sets --------------------------------------------------------

#: Verification capabilities the router can select. Higher capability sets are
#: strictly more expensive; the planner unions per-file routes so that selection
#: is monotonic.
CAP_QUALITY = "quality"
CAP_TESTS = "tests"
CAP_INSTALLER_CONTRACT = "installer_contract"
#: Package a single Windows x86_64 smoke (the cheapest native package that
#: exercises the installer path end to end).
CAP_PACKAGE_WINDOWS_X86_64 = "package_windows_x86_64"
#: Package all five native targets.
CAP_PACKAGE_ALL = "package_all"

_FULL = {
    CAP_QUALITY,
    CAP_TESTS,
    CAP_INSTALLER_CONTRACT,
    CAP_PACKAGE_ALL,
}


# --- Path routing ----------------------------------------------------------

def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern (``*`` / ``?`` / ``**``) into a regex.

    Matching is anchored and rooted at the repository root: ``core/**`` matches
    any path whose first component is ``core``. ``**`` matches across
    separators, a plain ``*`` does not. This mirrors fnmatch behaviour without
    the fragile ``Path.match`` partial-suffix quirks.
    """
    pattern = pattern.lstrip("/")
    parts = pattern.split("/")
    regex_parts: list[str] = []
    for part in parts:
        if part == "**":
            regex_parts.append(r"(?:[^/]+(?:/[^/]+)*)?")
            continue
        if "**" in part:
            raise ValueError(f"Unsupported glob pattern {pattern!r}: '**' must be a whole path component")
        regex_parts.append(
            "".join(
                ".*" if char == "*" else "." if char == "?" else re.escape(char)
                for char in part
            )
        )
    return re.compile("/".join(regex_parts) + r"(?:/.*)?\Z")


def _match(patterns: tuple[str, ...], path: str) -> bool:
    """Return True if ``path`` matches any of the anchored glob patterns."""
    if not path:
        return False
    for pattern in patterns:
        if _compile_pattern(pattern).match(path):
            return True
    return False


def _route(path: str) -> set[str]:
    """Return the verification capability set required for one changed path."""
    # Full packaging/build-system infrastructure changes.
    if _match(
        (
            "scripts/build_nuitka.py",
            "scripts/prepare_ffmpeg.py",
            "packaging/ffmpeg/**",
            "packaging/assets/**",
            "requirements-build.txt",
            "requirements.txt",
            "pyproject.toml",
            "FFmpeg/**",
            ".github/workflows/**",
            "scripts/ci_plan.py",
            "test/test_ci_plan.py",
            "test/test_release_workflows.py",
        ),
        path,
    ):
        return set(_FULL)

    # Windows installer route: the manifest and the Setup builder.
    if _match(
        ("packaging/windows/**", "scripts/build_setup.py"),
        path,
    ):
        return {
            CAP_QUALITY,
            CAP_INSTALLER_CONTRACT,
            CAP_PACKAGE_WINDOWS_X86_64,
        }

    # Documentation / metadata only.
    if _match(
        ("docs/**", "README.md", "LICENSE", "AGENTS.md", ".gitignore"),
        path,
    ):
        return {CAP_QUALITY}

    # Application / runtime code.
    if _match(
        ("main.py", "cli/**", "core/**", "gui/**", "config/**", "test/**"),
        path,
    ):
        return {CAP_QUALITY, CAP_TESTS}

    # Unknown path: fail closed with full verification.
    return set(_FULL)


# --- Plan construction -----------------------------------------------------

def _plan_for_paths(changed_paths: list[str]) -> dict:
    features: set[str] = set()
    for path in changed_paths:
        if path:
            features |= _route(path)

    # An empty/unknown set is not a reason to weaken verification: fail closed
    # to full so a routing gap can never silently under-test.
    if not changed_paths or not features:
        features = set(_FULL)

    has_tests = CAP_TESTS in features
    run_installer_contract = CAP_INSTALLER_CONTRACT in features
    has_packages = CAP_PACKAGE_ALL in features or CAP_PACKAGE_WINDOWS_X86_64 in features

    if CAP_PACKAGE_ALL in features:
        package_targets = list(PACKAGE_TARGETS.values())
    elif CAP_PACKAGE_WINDOWS_X86_64 in features:
        package_targets = [PACKAGE_TARGETS["windows-x86_64"]]
    else:
        package_targets = []

    package_mode = DEFAULT_PACKAGE_MODE
    test_matrix = list(TEST_TARGETS.values()) if has_tests else []

    summary = _human_summary(
        changed_paths=changed_paths,
        has_tests=has_tests,
        run_installer_contract=run_installer_contract,
        package_matrix=package_targets,
        package_mode=package_mode,
    )

    return {
        "has_tests": has_tests,
        "test_matrix": test_matrix,
        "run_installer_contract": run_installer_contract,
        "has_packages": has_packages,
        "package_matrix": package_targets,
        "package_mode": package_mode,
        "summary": summary,
    }


def _human_summary(
    changed_paths: list[str],
    has_tests: bool,
    run_installer_contract: bool,
    package_matrix: list[dict[str, str]],
    package_mode: str,
) -> str:
    selected: list[tuple[str, bool]] = [
        ("quality", True),
        ("unit-test matrix", has_tests),
        ("Windows installer contract", run_installer_contract),
        (f"package {package_mode}", bool(package_matrix)),
    ]
    selected_text = "\n".join(
        ("✓ " if enabled else "○ ") + name for name, enabled in selected
    )
    package_text = ", ".join(target["target"] for target in package_matrix) or "(none)"

    if not changed_paths:
        reason = "No changed paths supplied to the planner; failing closed to full."
    else:
        reason = f"Changed paths triggered the {package_text or 'quality-only'} route."

    return (
        "Verification plan\n\n"
        "Changed:\n" + "\n".join(f"- {path}" for path in changed_paths) + "\n\n"
        "Selected:\n" + selected_text + "\n\n"
        f"Packages: {package_text}\n\n"
        f"Reason: {reason}"
    )


# --- Output helpers --------------------------------------------------------

def _github_output_value(value) -> str:
    """Encode a value so it is safe to write into GITHUB_OUTPUT."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        return escaped
    # Lists/dicts are emitted as a single-line compact JSON string.
    return json.dumps(value, separators=(",", ":"))


def write_github_outputs(
    plan: dict,
    *,
    output_path: str | os.PathLike[str] | None = None,
    summary_path: str | os.PathLike[str] | None = None,
) -> None:
    if output_path is None:
        output_path = os.environ.get("GITHUB_OUTPUT")
    if summary_path is None:
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if output_path:
        lines = [
            f"has_tests={_github_output_value(plan['has_tests'])}",
            f"test_matrix={_github_output_value(plan['test_matrix'])}",
            f"run_installer_contract={_github_output_value(plan['run_installer_contract'])}",
            f"has_packages={_github_output_value(plan['has_packages'])}",
            f"package_matrix={_github_output_value(plan['package_matrix'])}",
            f"package_mode={_github_output_value(plan['package_mode'])}",
        ]
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(plan["summary"] + "\n")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    plan = _plan_for_paths(args)

    # The machine-readable JSON contract is printed to stdout so a caller can
    # capture it directly; the human-readable plan goes to the step summary.
    contract = {
        "has_tests": plan["has_tests"],
        "test_matrix": plan["test_matrix"],
        "run_installer_contract": plan["run_installer_contract"],
        "has_packages": plan["has_packages"],
        "package_matrix": plan["package_matrix"],
        "package_mode": plan["package_mode"],
    }
    print(json.dumps(contract, separators=(",", ":")))

    write_github_outputs(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
