# QualityBound architecture

QualityBound separates platform-neutral compression behavior from its CLI and
PySide6 GUI. `main.py` is the composition root.

```text
main.py
├── cli/       command-line entrypoint and presentation
├── gui/       PySide6 entrypoint and presentation
└── core/
    ├── config/    paths, settings, presets, and translations
    ├── media/     source discovery, subtitles, and file validation
    ├── ffmpeg/    discovery, probes, capabilities, and commands
    ├── smart/     Scout, VMAF measurement, search, and decisions
    └── encoding/  planning, execution, and concurrent scheduling
```

## Dependency direction

- `core` imports only the standard library and other `core` modules.
- `cli` imports the standard library, `cli`, and `core`, never `gui`.
- `gui` imports the standard library, `gui`, `core`, and Qt, never `cli`.
- Shared behavior belongs in the lowest suitable layer and dependencies remain
  acyclic.

The recursive architecture test enforces the root allowlist, capability-package
DAG, public adapters, and layer boundaries.

## Encoding ownership

The planner binds each item to one concrete encoder before Smart analysis.
Parallel workers receive deep-copied plan items; mutable `EncodePlanItem`
instances are never shared across workers.

Smart owns sampling, measurement identity, bitrate search, holdout refinement,
constraint decisions, and receipt persistence. Encoding owns plan construction,
full-file execution, concurrency, and publication of validated temporary files.

## Release slice

Build and packaging helpers live under `scripts/`; installer and release assets
live under `packaging/`; GitHub Actions owns native validation and publication.
See [Release map](release-map.md) for the artifact contracts and
[CI workflows](ci-workflows.md) for automatic routing.
