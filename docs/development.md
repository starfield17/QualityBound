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

## Deferred check work

Two known gaps are left open on purpose. None of them blocks a release. Each is
recorded with the measurement that produced it, so it can be picked up without
re-running the audit.

### Strict-mode inference debt at the dict and JSON boundaries

`pyproject.toml` turns off six strict rules: `reportMissingTypeArgument`,
`reportUnknownArgumentType`, `reportUnknownLambdaType`, `reportUnknownMemberType`,
`reportUnknownParameterType`, `reportUnknownVariableType`. Measured over the current
scope (`core`, `cli`, `gui`, `main.py`) they hold back 434 diagnostics: 312 in
`core`, 112 in `gui`, 10 in `cli`. The dominant pattern is capability snapshots,
preset documents and analysis receipts travelling as `dict[str, object]` or
JSON-decoded mappings, so the checker cannot name the types that flow through them.

Closing it means naming those boundaries inside `core` — a `TypedDict` or dataclass
per receipt kind, capability snapshot and preset document — and letting the adapters
build them. A restructuring of that size needs its own diff and independent review,
so it was not folded into the strict-mode adoption.

Re-measure the residue with a throwaway config that copies the `include`/`exclude`
lists from `pyproject.toml` and sets those six rules to `"warning"`, then read the
summary:

```bash
python -m pyright -p .debt-probe.json --outputjson \
  | python -c "import json,sys; print(json.load(sys.stdin)['summary'])"
```

The annotations added for the strict adoption (`gui/qt_optionals.maybe_none`,
`QueueTableModel._transient_index`, `core.smart.v1.bitrate._SharedSearchResultFields`,
the `__all__` lists in `core/encoding`) are not suppressions. They stay checked by
the remaining strict rules, and the `__all__` lists keep working after the six rules
come back on.

### `scripts/` is outside the type-check scope

`pyproject.toml` includes `core`, `cli`, `gui` and `main.py`. Adding `scripts`
analyses 8 files and reports 9 findings in basic mode over 3 of them:

- `scripts/build_icons.py` — the Pillow `save(format=...)` overload rejects
  `Literal["PNG"]`, and a `QByteArray` is passed where `Iterable[SupportsRead]`
  is expected (the Qt form is `bytes(state.toBase64().data())`);
- `scripts/prepare_ffmpeg.py` — two `IO[bytes]` arguments against `BinaryIO`
  parameters, and one iteration over a value typed `object`;
- `scripts/run_smart_case.py` — two optional values (`bool | None`, `int | None`)
  passed to non-optional parameters.

These are development and packaging tools, not application payload, so the scope
choice is a cost decision rather than a safety one. They are not unchecked, only
untyped: `build_icons.py` runs in the quality gate, `prepare_ffmpeg.py` runs in the
packaging workflow, and `run_smart_case.py` is exercised by `test/test_smart_case.py`
and `test/test_smart_corpus.py`.

## Known limitations

### MP4 output cannot carry bitmap subtitles

`core/ffmpeg/commands.py:build_subtitle_args` maps the source's subtitle
streams into MP4 with `-map 0:s? -c:s mov_text`. `mov_text` is a text codec, so
it only accepts text subtitle sources (SubRip, ASS, WebVTT, ...). A source whose
subtitle stream is a bitmap format — PGS (`hdmv_pgs_subtitle`), DVD/VobSub
(`dvd_subtitle`) or DVB (`dvbsub`) — is rejected by FFmpeg before the output
file is opened:

```text
[sost#0:1/mov_text @ ...] Subtitle encoding currently only possible from text to text or bitmap to bitmap
Error opening output file out.mp4.
Error opening output files: Invalid argument
```

The item then fails with that FFmpeg message in `error_message` and no output is
published. MKV output is unaffected: it copies the stream with `-c:s copy`, so
bitmap subtitles survive as they are. The behaviour was reproduced with a
one-display-set PGS stream, and the text path (`.srt` to MP4 `mov_text`) encodes
successfully.

Nothing in planning probes the subtitle codecs, so the failure is only surfaced
after the full video encode has run. Deciding what to do needs a product choice,
because the three candidate behaviours are not equivalent:

- drop the bitmap subtitle stream for MP4 (encode succeeds, subtitles are lost);
- fail early during planning when a bitmap subtitle would target MP4; or
- convert MP4 bitmap-subtitle output to MKV, which changes the requested
  container.

Until one is chosen, users who need bitmap subtitles should select MKV.

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
