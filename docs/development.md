# Developing QualityBound

## Environment

Install runtime and development dependencies from the repository root:

```bash
python -m pip install -r requirements-dev.txt
```

Run the GUI or CLI directly:

```bash
python main.py
python main.py --cli --help
```

`launch.sh` and `launch.bat` provide convenience launchers. On Windows,
`launch.bat` prefers the active Conda environment when available.

## FFmpeg

Smart mode requires an FFmpeg build whose `libvmaf`, `siti`, and `scdet`
filters can run. An explicitly supplied FFmpeg/FFprobe pair always takes
priority and is never replaced by a system fallback. Otherwise QualityBound
checks the repository `FFmpeg/` directory before the system path.

Supported bundled layouts are `FFmpeg/ffmpeg` plus `FFmpeg/ffprobe`, or the same
pair under `FFmpeg/bin/`, with `.exe` suffixes on Windows.

## Translations

Built-in language packs live in `config/i18n/`. English is the complete
baseline and every built-in pack covers the same keys. User language packs are
partial JSON overrides placed in the writable `translations/` directory.

In a source checkout that directory is `<repo>/translations/`. In the macOS app
it is `~/Library/Application Support/QualityBound/translations/`. A language
pack must provide a string `language.name`; invalid entries are skipped with a
startup diagnostic and missing entries fall back to English.

The QualityBound product name is not translated. Locale packs use `QualityBound`
for `app.title`.

## Validation

```bash
ruff check .
pyright
python -m unittest discover -s test -p "test_*.py" -v
python scripts/build_icons.py --check
```

Architecture checks can be run separately:

```bash
python -m unittest discover -s test -p "test_architecture.py" -v
```

## Smart analysis and queue completion

GUI intake discovers and probes sources into `AWAITING_START` records containing
source drafts, with no execution plan or tool snapshot. Directory recursion is
resolved during intake; Start does not rescan the folder. `QueuePrepareWorker`
freezes current options, output directory, tools and workdir at Start, preferring
each source's independent parameter and directory overrides. Metadata is probed
again using the start-time tools to account for source changes. Parameter edits
and output naming/validation do not run during intake or right-click override
selection.

Preparation locks edits, reordering and decision activation. The model commits
the complete prepared batch after validating identities and output collisions;
Stop or close before handoff leaves drafts unbound and starts no encodes. Global
tool/encoder or queue collision errors leave the batch awaiting correction;
per-file probe/output validation errors become Failed records, while valid items
run. A pause or Smart decision resumes the bound plan. Manual retry returns to
the draft and preserves its overrides, selecting current global settings at the
next Start. The obsolete GUI `PlanWorker` and eager planning callback are removed.

When resumed and new items carry different tool snapshots, contiguous groups use
their own FFmpeg/FFprobe/workdir. All groups finish analysis before encoding;
groups encode in queue order with at most the configured concurrency. The core
executors accept matching analysis terminal results and do not measure a group
again during the encode phase. Ready Smart items still require successful
analysis and the usual final validation/publication gates.

Both Smart algorithms use the same analysis-policy resolver. A size conflict
first applies the selected size or quality relaxation to measured candidates.
If it remains size-blocked with a required output ratio greater than 1.0, it
becomes an intentional `SMART_PREDICTED_OVERSIZE` skip. An unknown ratio or a
ratio equal to 1.0 remains a decision when no configured relaxation succeeds.
An unreachable quality target follows the separate Skip / Ask policy; a failed
holdout quality check belongs to this outcome, while measurement and tool errors
remain failures. Actual full-encode size misses retain their preserved-file
decision lifecycle and never use the prediction skip rule.

The queue emits an execution-stopped handoff even if some items need decisions.
MainWindow asks the size-conflict and quality-miss questions, then the completion
handler applies Copy / Ask / Ignore to intentional skips. Cancelling an analysis
question leaves that item pending without blocking other source copies. The same
analysis result is not automatically asked again during that run; reanalysis can
produce a new question. A source-copy refusal records an ignored outcome.

Copies operate on per-item snapshots in a background worker, through an adjacent
temporary file and atomic publication. Exclusive publication protects an output
that appears during copying when overwrite is disabled. Stop cancels the current
copy and removes its temporary file; completed outcomes survive a resumed run.
Copy failures update the item to Failed with a diagnostic. The GUI stays busy
through this handoff, and only schedules newly executable items after it finishes.
Final reports and post-run actions wait until decisions are resolved.

Regression coverage includes fresh default configuration without opening Settings,
mixed pending/skipped batches, the strict greater-than-source threshold in both
algorithms, holdout policy routing, repeated completion, cancellation and output
publication races. These synthetic tests verify control flow and file side effects,
not real-world VMAF accuracy.

## Checks deliberately not adopted

### `reportImplicitStringConcatenation`

Measured as a warning over the application scope (`core`, `cli`, `gui`, `main.py`)
the rule reports 19 findings across 14 files, and every one is an intentional
multi-line string, not an omitted comma:

- a sentence split to stay readable, such as `cli/cli_entry.py:145`
  (`"...--backend auto. " "Choose a concrete backend..."`);
- an f-string continued on the next line inside a call, such as
  `core/encoding/analysis.py:149`;
- the two halves of a filtergraph, such as `core/smart/v1/vmaf.py:301`;
- `_write_analysis_header` (`core/smart/v1/workflow.py:128`), a 15-literal block
  of `key=value\n` lines, plus the two smaller log blocks it resembles.

Adopting the rule means rewriting each site with `+` or replacing the log blocks
with `str.join`, so a readable message or log header would be made less readable
to guard against a defect class the audit found zero instances of. `ruff` already
declines the style families for the same reason, and this repository wants the
bug-finding subset instead (`pyproject.toml`).

Re-measure by temporarily adding `"reportImplicitStringConcatenation": "warning"`
to the project `include` scope. If a future finding is a genuine concatenation
defect, that site is worth fixing whether or not the rule is adopted.

## Deferred check work

Two known gaps are left open on purpose. Neither of them blocks a release. Each is
recorded with the measurement that produced it, so it can be picked up without
re-running the audit. The scope of each check — strict for the application, basic
for `test/` and `scripts/` — is recorded with its reason in the same section.

### Strict-mode inference debt at the dict and JSON boundaries

`pyproject.toml` turns off six strict rules: `reportMissingTypeArgument`,
`reportUnknownArgumentType`, `reportUnknownLambdaType`, `reportUnknownMemberType`,
`reportUnknownParameterType`, `reportUnknownVariableType`. Measured over the current
scope (`core`, `cli`, `gui`, `main.py`) they hold back 610 diagnostics: 483 in
`core`, 116 in `gui`, 11 in `cli`. The dominant pattern is capability snapshots,
preset documents and analysis receipts travelling as `dict[str, object]` or
JSON-decoded mappings, so the checker cannot name the types that flow through them.

That number is ratcheted rather than merely written down. `pyright.strict-debt.json`
extends the project config and raises those six rules to warnings;
`scripts/check_strict_debt.py` measures through it and fails the Quality job when
the total or any package count grows above the baseline recorded in that script.
Shrinking is always allowed, and the new numbers are recorded in both places:

```bash
python scripts/check_strict_debt.py
```

Closing it means naming those boundaries inside `core` — a `TypedDict` or dataclass
per receipt kind, capability snapshot and preset document — and letting the adapters
build them. A restructuring of that size needs its own diff and independent review,
so it was not folded into the strict-mode adoption.

The annotations added for the strict adoption (`gui/qt_optionals.maybe_none`,
`QueueTableModel._transient_index`, `core.smart.v1.bitrate._SharedSearchResultFields`,
the `__all__` lists in `core/encoding`) are not suppressions. They stay checked by
the remaining strict rules, and the `__all__` lists keep working after the six rules
come back on.

### The test suite is type-checked in `basic` mode, not `strict`

`pyright.tests.json` includes `test/` and runs the project config in `basic` mode
with `reportUnnecessaryTypeIgnoreComment`, so an unnecessary `type: ignore` in a
test is an error rather than an unchecked comment. The Quality job runs it after
the strict pass over the application.

`strict` is not available for `test/` from the project config: pyright resolves
`typeCheckingMode` per configuration file, not per directory, so a stricter pass
would apply the same rules to both scopes. A strict pass over `test/` reports
findings in three classes, and the first two are intrinsic to testing rather than
fixable defects:

- `reportPrivateUsage` — tests deliberately call the module-private helpers they
  cover (`_run_command`, `_validate_decode_acceleration`, `_imports`);
- `reportMissingParameterType` — fixture helpers and fake callbacks annotate
  nothing when the value is a throwaway;
- `reportArgumentType` and `reportOptionalMemberAccess` on the `dict[str, object]`
  receipt and event payloads, the same boundary the application's strict pass
  leaves to the disabled inference rules.

Re-measure with `pyright -p pyright.tests.json` after temporarily raising
`typeCheckingMode` in that file to `strict`. Closing the gap means either a
per-directory rule policy (not supported today) or splitting the test tree across
two configuration files, so the decision is recorded here rather than taken
silently.

### `scripts/` is type-checked in `basic` mode

`pyright.scripts.json` includes `scripts/` and runs the project settings in basic
mode. It reports nothing now; the nine findings it started with were three real
typing mistakes rather than style:

- `scripts/build_icons.py` passed a `str` to `QImage.save(device, format)`, which
takes `bytes`, and returned `QByteArray` where `bytes` was declared — now
  `b"PNG"` and `bytes(encoded.data())`;
- `scripts/prepare_ffmpeg.py` typed `_copy_stream` as `BinaryIO` while `zipfile`
  and `tarfile` hand it `IO[bytes]`, and iterated `data["licenses"]` without
  narrowing it out of `object`;
- `scripts/run_smart_case.py` passed the possibly-unset `ground_truth_passed` and
  `full_encode_output_bytes` into `OraclePoint`, which declares them non-optional.

Strict mode is not available for this scope: a strict pass over `scripts/` reports
147 findings, 139 of them the same six disabled inference rules the application
scope leaves open (capability snapshots, preset documents and JSON-decoded
manifests travelling as `object`). The remaining eight are one missing stub for
`nuitka`, two unannotated parameters, one argument-type mismatch over a JSON union,
and dead `is not None` guards. These are development and packaging tools, not
application payload, so the scope choice is a cost decision rather than a safety
one; the executables still run in the quality gate (`build_icons.py --check`), the
packaging workflow (`prepare_ffmpeg.py`, `build_nuitka.py`), and
`test/test_smart_case.py` / `test/test_smart_corpus.py`.

## Known limitations

### MP4 output cannot carry bitmap subtitles

`core/ffmpeg/commands.py:build_subtitle_args` maps the source's subtitle
streams into MP4 with `-map 0:s? -c:s mov_text`. `mov_text` is a text codec, so
it only accepts text subtitle sources (SubRip, ASS, WebVTT, ...). A source whose
subtitle stream is a bitmap format — PGS (`hdmv_pgs_subtitle`), DVD/VobSub
(`dvd_subtitle`), DVB (`dvbsub`), DVB teletext or XSUB — is rejected by FFmpeg
before the output file is opened:

```text
[sost#0:1/mov_text @ ...] Subtitle encoding currently only possible from text to text or bitmap to bitmap
Error opening output file out.mp4.
Error opening output files: Invalid argument
```

The behaviour was reproduced with a one-display-set PGS stream, and the text
path (`.srt` to MP4 `mov_text`) encodes successfully.

Planning now refuses such a source before any encode runs, so the failure is not
left to the end of the video encode (`SPEC.md` N9 ← S3: dropping the stream would
publish a file that lost part of the input, and switching the container would
change the requested output). `core/media/subtitles.py` names the bitmap codecs
in `MP4_INCAPABLE_SUBTITLE_CODECS`; `core/media/validation.py:
validate_subtitle_carrier` applies them to `MediaInfo.subtitle_codecs`, which
`core/ffmpeg/probe.py` fills from the ffprobe stream list. The item becomes a
skipped plan item whose reason names the codec, with the source unchanged.

MKV output is unaffected: it copies the stream with `-c:s copy`, so bitmap
subtitles survive as they are. Users who need bitmap subtitles select MKV;
disabling subtitle copying also plans for MP4, and that choice is the
operator's, not a silent drop.

## Packaging

Install build requirements and create a native package on the current platform:

```bash
python -m pip install -r requirements-build.txt
python scripts/build_nuitka.py --clean --version 3.3.1
```

The normalized standalone directory is `dist/qualitybound/`. A native macOS app
build produces `dist/QualityBound.app/` and `dist/qualitybound.dmg`.

Tagged releases prepare and require the pinned FFmpeg pair before packaging.
The release matrix contains Windows x86-64/ARM64, Linux x86-64/ARM64, and macOS
arm64, producing eight platform packages. Read [Release map](release-map.md)
before changing build, packaging, or workflow behavior.
