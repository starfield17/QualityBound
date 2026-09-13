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

## Packaging

Install build requirements and create a native package on the current platform:

```bash
python -m pip install -r requirements-build.txt
python scripts/build_nuitka.py --clean --version 4.0.0
```

The normalized standalone directory is `dist/qualitybound/`. A native macOS app
build produces `dist/QualityBound.app/` and `dist/qualitybound.dmg`.

Tagged releases prepare and require the pinned FFmpeg pair before packaging.
The release matrix contains Windows x86-64/ARM64, Linux x86-64/ARM64, and macOS
arm64, producing eight platform packages. Read [Release map](release-map.md)
before changing build, packaging, or workflow behavior.
