# QualityBound GUI layer guide

`gui/` is the PySide6 presentation layer. It imports the standard library,
`gui`, Qt, and public `core` package contracts; never `cli` or `main`.

## Entrypoints and owners

- `gui.gui_entry` builds the translation catalog and is the only GUI module
  allowed to import `gui.gui_mainwindow.MainWindow`.
- MainWindow is the GUI composition root: source/output selection, menu,
  shortcuts, tray, geometry, configuration, workers and panel coordination.
  Its builder methods stay in the same class, grouped by task.
- `queue/` owns records, actions, model/view, workers and completion.
- `comparison/` owns independent read-only A/B inspection and playback.
- `options/` owns encoding controls, settings, presets and capability detection.
- `theme.py`, `icons.py`, `notification_banner.py`, `activity_log_window.py`
  and `window_geometry.py` are shared presentation owners.

The task index in `docs/architecture.md` links entry symbols to implementation,
contracts and focused tests. Subpackage initializers are empty; import concrete
GUI owners rather than loading unrelated panels through a facade.

## Shared presentation contracts

- `theme.py` owns palette-aware tokens and the application stylesheet.
  MainWindow or the GUI entrypoint calls `apply_theme()`; status colours come
  from `status_color()`. Toolbar icons refresh when the theme changes.
- Presentation widgets display state and emit actions without mutating records
  or starting work, including the notification banner and queue details panel.
- The queue is the primary workspace. Preserve `queue_stack`/`details_panel`
  and the collapsible setup card when changing the layout.
- The status bar keeps Stage, File, Speed, Elapsed and Current progress visible.
  Log messages go to Activity Log, never `QStatusBar.showMessage()`. Stage/state
  labels use `progress_stage_key()`/`progress_state_key()`.
- Search predictions carry `~` or `(est.)`; measured size/savings are separate.
  New text uses `config/i18n/`; translation sets buddies and accessible names.

## Checks

Pyright is strict over all GUI modules. Qt typing conventions are in
`docs/development.md`; the six disabled inference rules are in `pyproject.toml`.
Use `qt_optionals.maybe_none` for Qt runtime nulls whose stubs are non-nullable.

```text
python -m unittest discover -s test -p "test_architecture.py" -v
python -m unittest discover -s test -p "test_gui_presentation.py" -v
```

The root guide owns the complete validation commands and CI routing policy.
