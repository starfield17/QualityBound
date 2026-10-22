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
    ├── smart/     Scout, VMAF measurement, search, and decisions (v1/, v2/)
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

## GUI presentation

`gui/theme.py` owns palette-aware semantic colour tokens and the single
application-wide stylesheet; `gui/icons.py` renders the toolbar line-icon set.
`gui/notification_banner.py` and `gui/item_details_panel.py` are view-only
widgets that display queue state and emit user actions. `MainWindow` remains the
composition root: it owns the menu, shortcuts, tray menu, window geometry and the
collapsible queue-first layout. Presentation widgets never mutate queue records
or start encoding work.

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
