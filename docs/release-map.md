# Release map

This map covers the release vertical slice. Keep repository-wide architecture
rules in `AGENTS.md`; keep CI/Verify/Release orchestration in
`docs/ci-workflows.md`; keep platform implementation details near their
scripts.

## Entry points and ownership

| Path | Responsibility |
| --- | --- |
| `.github/workflows/ci.yml` | Automatic minimum verification: plans from changed files and routes Quality, the three native OS tests, the pinned-ISCC Windows installer contract, and the five native packaging smokes. The `CI Gate` job is the single required status. |
| `.github/workflows/verify.yml` | Workflow-dispatch verification with `fast`, `targeted`, `installer`, and `full` profiles so agents can add verification beyond the automatic minimum. |
| `.github/workflows/release.yml` | Gates numeric `v*` tags through Quality + three OS tests + installer contract, builds all five targets in `release` mode, verifies eight final packages, writes checksums, and creates or updates the GitHub Release. |
| `.github/workflows/_quality.yml` | Quality gate: architecture, workflow contract, planner contract, Windows Setup contract, ruff, pyright, compileall, generated icon check. |
| `.github/workflows/_test.yml` | One native OS unit-test environment. |
| `.github/workflows/_windows-installer-contract.yml` | Fast pinned-ISCC contract: compile `installer.iss` for x86_64 and ARM64 with a dummy payload. |
| `.github/workflows/_package.yml` | One native Nuitka package build in `smoke` / `candidate` / `release` mode. |
| `scripts/ci_plan.py` | Deterministic, stdlib-only routing policy. Single source of truth for `TEST_TARGETS` and `PACKAGE_TARGETS`. |
| `scripts/build_nuitka.py` | Owns Nuitka command construction, version normalization, staging, and platform output discovery. |
| `scripts/prepare_ffmpeg.py` | Downloads and validates the target-specific FFmpeg bundle described by `packaging/ffmpeg/manifest.json`. |
| `scripts/build_setup.py` | Validates the staged Windows tree and passes release, path, and architecture defines to Inno Setup. |
| `packaging/windows/installer.iss` | Owns the QualityBound Windows installer identity and per-user installation behavior. |
| `scripts/sign_windows.ps1` | Authenticode-signs the staged application executable and final Setup executable when release secrets exist. |
| `test/test_release_workflows.py` | Enforces the reusable-workflow layout, matrix targets, and cross-workflow artifact contracts. |
| `test/test_ci_plan.py` | Enforces the routing policy: canonical matrices, monotonicity, fail-closed unknown paths. |
| `test/test_windows_setup.py` | Enforces the Python-to-Inno command contract and installer manifest invariants. |

## Contracts

- CI, Verify, Release, and the planner keep the same five target names and
  native runners. The single source of truth is `scripts/ci_plan.py`; change
  targets there and in the contract tests together.
- The release matrix produces eight packages: two Windows ZIPs, two Windows
  Setup executables, two Linux tarballs, one macOS tarball, and one macOS DMG.
  Intel macOS is not a supported release target.
- `packaging/ffmpeg/manifest.json` schema v2 with verification contract v3 is
  the source of truth for FFmpeg URLs, checksums, licenses, exact
  FFmpeg/libvmaf commits, required Scout/VMAF filters, and build/mirror
  provenance. Every release target must have one matching manifest entry.
- `scripts/build_nuitka.py` is the source of truth for accepted numeric versions
  and generated package layout. Release tags use `vMAJOR.MINOR.PATCH` or the
  supported four-part numeric form.
- `packaging/windows/installer.iss` is the single source of truth for the new
  QualityBound Inno `AppId`. A literal GUID begins with `{{` because Inno
  escapes a literal left brace by doubling it. Do not inject the AppId with an
  ISCC `/D` define.
- QualityBound intentionally does not detect, remove, or upgrade legacy Video
  Compressor MSI/Inno installations. Users uninstall the old product manually.
- The installer contract installs Inno Setup 7.1.0 in portable mode under
  `workdir`, passes its exact `ISCC.exe` path to `build_setup.py`, and compiles
  `installer.iss` for x86_64 and ARM64 before any expensive Nuitka release
  packaging starts.
- Release packaging is always a fresh build from the tagged commit; it never
  reuses a CI artifact. Each `release` package job runs the pinned FFmpeg
  preparation, the real Nuitka build, and runtime/package validation — but not
  the full Python unittest suite, which belongs to `_test.yml`.
- A pushed release tag is immutable input to the workflow. Rerunning a failed
  workflow checks out the commit still referenced by the remote tag, not a newer
  commit on `main` or a locally moved tag.

## Verification

Run the local contract checks with the repository's Python environment:

```text
ruff check .
pyright
python -m unittest discover -s test -p "test_*.py" -v
```

CI first compiles the Inno manifest for x86_64 and ARM64 with a minimal dummy
payload using the explicitly selected portable ISCC 7.1.0. This fast contract
is the executable check for `.iss` syntax when ISCC is unavailable on the
development host. The later Windows packaging smoke jobs still compile the real
Nuitka payload, silently install it, inspect it, and silently uninstall it as
the end-to-end check.

Before tagging a release, run `Verify/full` (release rehearsal in `candidate`
mode) and confirm the target commit's main CI run succeeded. After pushing the
annotated tag, confirm all five build jobs, the publish job, the eight release
packages, and `SHA256SUMS.txt`.
