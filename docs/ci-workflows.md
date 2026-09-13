# CI / Verify / Release workflows

This document describes the GitHub Actions architecture after the workflow
refactor. The repository uses a small reusable workflow library: three entry
workflows orchestrate, four reusable workflows execute, and a deterministic
Python planner decides the automatic minimum verification set.

## Workflow layout

Entry workflows:

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `ci.yml` | push / pull_request / dispatch | Automatic minimum verification. |
| `verify.yml` | workflow_dispatch | Human- or agent-invoked verification. |
| `release.yml` | tag `v*` | Deterministic full release build and publication. |

Reusable workflows:

| Workflow | Responsibility |
| --- | --- |
| `_quality.yml` | Architecture contract, workflow contracts, planner contract, Windows Setup contract, ruff, pyright, compileall, generated icon check. Always runs on ubuntu-22.04 with Python 3.13. Never skippable. |
| `_test.yml` | One native OS unit-test environment. |
| `_windows-installer-contract.yml` | Pinned Inno Setup 7.1.0 portable, ISCC compile of `installer.iss` for x86_64 and ARM64 with a dummy payload. |
| `_package.yml` | One native Nuitka package build in `smoke` / `candidate` / `release` mode. |

## Routing policy

`scripts/ci_plan.py` is the only routing policy. It is stdlib-only and
deterministic. It maintains two canonical matrices: `TEST_TARGETS`
(linux / windows / macos unit-test environments) and `PACKAGE_TARGETS`
(the five native packaging targets). Workflow contract tests read those
matrices, so a target change happens only in this file.

Per changed path the planner selects a capability set, then unions them
(monotonic: no low-risk file can downgrade a high-risk one):

| Changed path | Plan |
| --- | --- |
| `docs/**`, `README.md`, `LICENSE`, `AGENTS.md`, `.gitignore` | Quality only. |
| `main.py`, `cli/**`, `core/**`, `gui/**`, `config/**`, `test/**` | Quality + 3 OS tests. No Nuitka packaging. |
| `packaging/windows/**`, `scripts/build_setup.py` | Quality + installer contract + `windows-x86_64` package smoke. |
| `scripts/build_nuitka.py`, `scripts/prepare_ffmpeg.py`, `packaging/ffmpeg/**`, `packaging/assets/**`, `requirements*.txt`, `pyproject.toml`, `FFmpeg/**`, `.github/workflows/**`, `scripts/ci_plan.py`, `test/test_ci_plan.py`, `test/test_release_workflows.py` | Full: Quality + 3 OS tests + installer contract + all five package smokes. |
| Anything unknown | Full (fail closed). |

The planner outputs a compact JSON contract to `GITHUB_OUTPUT`
(`has_tests`, `test_matrix`, `run_installer_contract`, `has_packages`,
`package_matrix`, `package_mode`) and a human-readable plan to
`GITHUB_STEP_SUMMARY` for agent debugging.

`ci.yml` runs the plan on the changed files (PR base vs head, or
`github.event.before` vs head), then routes the selected reusable workflows.
The `CI Gate` job always runs, validates the planner wiring (a required job
that was skipped fails the gate), and is the single status branch protection
should require.

## Verify profiles

`verify.yml` exposes four profiles plus eight native toggles:

| Profile | Runs |
| --- | --- |
| `fast` | Quality + Linux tests. |
| `targeted` | Quality + selected tests/packages. Selecting any Windows package also enables the installer contract. Selecting nothing fails (never silently quality-only). |
| `installer` | Quality + Windows installer contract. |
| `full` | Quality + 3 OS tests + installer contract + all five packages in `candidate` mode (release rehearsal). |

Agents may dispatch Verify to add checks. They may never reduce the automatic
minimum selected by the repository policy.

## Package modes

`_package.yml` runs the same implementation with different intent:

| Mode | FFmpeg | Signing | Artifacts |
| --- | --- | --- | --- |
| `smoke` | Dummy bundled FFmpeg, no pinned download | No | None (Nuitka report only). |
| `candidate` | Pinned native FFmpeg, `--require-ffmpeg`, full runtime + package validation | No | None (release rehearsal). |
| `release` | Pinned native FFmpeg, `--require-ffmpeg`, full validation | Optional Windows signing | Archived packages uploaded as `release-package-*`. |

## Release

`release.yml` is deterministic and complete. It does not reuse CI artifacts:
it checks out the tagged SHA and builds fresh. Quality, the three OS tests,
and the installer contract must pass (`preflight`) before the five native
`release` builds start. The package jobs no longer run the full unittest
suite (tests belong to `_test.yml`). Publish validates the exact eight release
packages, writes `SHA256SUMS.txt`, and creates or updates the GitHub Release.

The Windows upload glob covers both the target archive and `-setup.exe`; it
must keep matching
`qualitybound-${{ github.ref_name }}-${{ inputs.target }}-setup.exe`.

## Local acceptance

Run the repository checks with the `Lab` environment:

```text
python -m unittest discover -s test -p "test_ci_plan.py" -v
python -m unittest discover -s test -p "test_release_workflows.py" -v
python -m unittest discover -s test -p "test_windows_setup.py" -v
python -m unittest discover -s test -p "test_architecture.py" -v
python -m unittest discover -s test -p "test_*.py" -v
ruff check .
pyright
python -m compileall -q main.py cli core gui scripts test
python scripts/build_icons.py --check
```
