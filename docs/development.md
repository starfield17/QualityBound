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

## Deferred check work

Three known gaps are left open on purpose. None of them blocks a release. Each is
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

### Unreferenced worker classes in `gui/gui_workers.py`

Two of the four are never instantiated anywhere in `gui/`, `cli/`, `core/`, `scripts/`
or `test/`:

- `EncodeWorker` — full-queue encoding is driven by `gui/queue_manager.py` calling
  `core.encoding.execute_plan_concurrent`. The class survives only in the import at
  `gui/gui_mainwindow.py:74` and in the `PlanWorker | EncodeWorker` annotation of
  `_start_worker` (`gui/gui_mainwindow.py:997`), which is called only with
  `PlanWorker`.
- `ScanWorker` — no references at all.

Deleting them is a live-code decision rather than a lint fix: `EncodeWorker` still
carries a single-file cancel-and-terminate path (`threading.Event` plus the recorded
`Popen`) that the queue runner has no equivalent of, so the question is whether that
capability is worth keeping around unused. `ScanWorker` has no such argument.

The mechanical part is: delete the classes, drop the import, and narrow the
`_start_worker` annotation to `PlanWorker`. `PlanWorker` declares the same five
signals as `EncodeWorker` (`completed`, `failed`, `cancelled`, `log`, `progress`),
so once the union collapses the `hasattr(worker, "log")`,
`hasattr(worker, "progress")` and `hasattr(worker, "cancelled")` guards in
`_start_worker` become always-true and should be replaced by direct connections in
the same change.

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

## Packaging

Install build requirements and create a native package on the current platform:

```bash
python -m pip install -r requirements-build.txt
python scripts/build_nuitka.py --clean --version 3.2.0
```

The normalized standalone directory is `dist/qualitybound/`. A native macOS app
build produces `dist/QualityBound.app/` and `dist/qualitybound.dmg`.

Tagged releases prepare and require the pinned FFmpeg pair before packaging.
The release matrix contains Windows x86-64/ARM64, Linux x86-64/ARM64, and macOS
arm64, producing eight platform packages. Read [Release map](release-map.md)
before changing build, packaging, or workflow behavior.
