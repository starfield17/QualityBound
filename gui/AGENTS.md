# QualityBound GUI layer guide

`gui/` is the PySide6 UI layer. It may import `core` and the standard library, and
other `gui` modules — never `cli`, and nothing above it in the dependency graph
(`main.py` is the composition root).

## Entrypoint and composition

- `gui.gui_entry` is the only GUI entrypoint and the only module allowed to import
  `gui.gui_mainwindow`. It builds the `TranslationCatalog`, parses `--lang` against
  it, and constructs `MainWindow`.
- `MainWindow` is the composition root for the GUI: it owns source/output selection,
  presets, workers, the queue, Settings, and the Activity Log, and coordinates the
  sub-panels.
- No other `gui` sub-module may import `gui.gui_mainwindow`.

## EncodeOptionsPanel

`gui.encode_options_panel.EncodeOptionsPanel(QWidget)` owns the Basic / Video /
Audio-Subtitles tabs and all internal wiring: codec/backend
filtering from runtime capabilities, encoder-preset refresh, analysis-profile state,
and Smart/Fixed control syncing.

- `MainWindow` interacts only through the public contract:
  `read_options()`, `apply_options()`,
  `apply_analysis_profile_settings()`, `current_analysis_profile_name()`,
  `sync_dependent_controls()`,
  `set_runtime_capabilities()`, `notify_capability_detection_failed()`,
  `begin_capability_detection()`, `set_translator()`, `set_busy()`,
  `set_visible()`, `summary_text()`.
- Semantic signals: `codec_changed`, `compression_mode_changed`,
  `analysis_profile_changed`, `options_changed`, `smart_unavailable`.
- Display labels for codec, container, backend, audio mode and log level live in
  `EncodeOptionsPanel`/`SettingsDialog`; the combo item data keeps the raw enum
  value. Do not read enum choices from `currentText()`.
- `MainWindow` must not reach into the panel's raw widgets; tests that need widget
  state access them via `window.options_panel.<widget>`.
- The panel renders the capability snapshot produced by `EncoderCapabilityDetectWorker`.
  Encoder entries include `preset_choices`; never run FFmpeg discovery or encoder
  probing synchronously from a widget refresh path.

## Queue layering

- `gui.queue_state` — Qt-free queue record/status/metrics logic. Imports `core` only.
- `gui.queue_actions` — Qt-free quality/size decision and file-side-effect actions
  over one queue record.
- `gui.queue_model` — `QueueTableModel`, `QueueColumn` and column metadata, cell
  formatting, roles and Qt notifications. Delegates mutations to `queue_state` and
  `queue_actions`; never owns receipt, constraint or file side effects.
- `gui.queue_view` — `ResponsiveQueueTableView`, header resize modes, reflow, and the
  `create_queue_view()` factory. May use `gui.queue_model`'s column definitions.
- `gui.queue_manager` — Qt worker/thread orchestration over the model.
- `gui.gui_workers` — the one-shot threads `MainWindow` starts directly:
  `QueueIntakeWorker` discovers/probes sources without encoding parameters;
  `QueuePrepareWorker` binds a batch at Start Queue; and
  `EncoderCapabilityDetectWorker` probes available encoders.
- New source records are `AWAITING_START`, with a source draft and no execution
  plan or tool snapshot. Start freezes current options and output directory,
  independently preferring per-item overrides. The model commits prepared plans
  atomically; preparation locks queue edits, reordering and decision activation.
  Resume retains its bound snapshot. Manual retry returns to source-draft state.
- `gui.queue_completion` — skipped-source publishing, reports, notifications and
  confirmed post-run actions. `process_stopped` handles skipped-source policy
  independently of unresolved decisions and copies snapshots on a background
  worker; result callbacks update the model on the GUI thread. Completed copy or
  ignore outcomes are not processed again when the run resumes.
  `QueueCompletionHandler.handle(records, translator,
  config)` receives only the completed run's records and current settings.
  MainWindow supplies log/notify/close callbacks; the handler uses its parent
  widget only for dialog ownership and never accesses MainWindow members.
- The global `encode_workers` application setting controls concurrent full-file
  encodes. Queue workers preserve each plan item's encoder binding; concurrency
  is not a preset option and does not select or rotate backends.
- View may depend on the model; model and state must not depend on the view.

## Video comparison

- `gui.comparison_window` owns independent A/B playback windows,
  scrubbing, audio selection, and cancellable background inspection workers.
- `gui.comparison_state` owns Qt-free timeline/offset and frame-selection rules;
  `gui.comparison_decode` owns read-only FFmpeg/FFprobe inspection and static-frame
  decoding. `gui.comparison_playback` owns the shared clock, external FFmpeg
  video/PCM audio pipes, and native Qt PCM output. Workers never mutate queue
  records or publish media. The codec ownership boundary is `SPEC.md` N11.
- MainWindow supplies paths, bound tools and translations. Comparison modules
  never import MainWindow or the queue. A generation token rejects stale decode
  results after seeks, file replacement, playback or closing.

## Presentation layer

- `gui.theme` owns the palette-aware light/dark tokens and the application-wide
  stylesheet. `apply_theme()` must be called through `MainWindow` or the GUI
  entrypoint; do not reintroduce a light-only per-widget stylesheet. Status
  colours come from `gui.theme.status_color()`.
- `gui.icons` renders the internal line-icon set for toolbar actions. Icons are
  regenerated when the application theme changes.
- `gui.notification_banner` and `gui.item_details_panel` are presentation-only:
  they may read queue records and emit actions, but they never mutate a record or
  start work themselves.
- The queue is the primary workspace. The setup card collapses to a compact
  summary; the empty state, queue table and selected-item details share the
  remaining height. Preserve the `queue_stack`/`details_panel` composition points
  when changing the layout.
- The status bar always keeps Stage, File, Speed, Elapsed and Current progress
  visible. Log messages go to `ActivityLogWindow`; they must not call
  `QStatusBar.showMessage()` or replace the live state. Stage/state progress
  labels are translated through `progress_stage_key()`/`progress_state_key()`.
- Predicted analysis values are marked as estimates (`~`, `(est.)`); measured
  output size and saved space are shown separately. Do not label a search
  prediction as measured.
- `MainWindow` owns the menu bar, shortcuts, tray menu and window-geometry
  persistence. New user-visible text goes through `config/i18n/`; label buddies
  and accessible names are set in the translation pass.

## MainWindow scope

`MainWindow` remains the GUI composition root and owns source selection, queue
interaction and platform notification delivery. QueueManager decides when a run
has completed; MainWindow resolves that run's IDs and delegates completion work
to QueueCompletionHandler. UI construction remains in same-class builder methods.

Pyright runs `strict` over `core`, `cli`, `main.py` and every module under `gui`;
the scope and the six disabled inference-precision rules live in `pyproject.toml`
(those rules cover the `dict[str, object]` and JSON boundaries, and closing them
means re-typing that boundary in `core`). Four GUI conventions exist because
Pyright rejects the alternatives:

- each widget stores its application translator as `self.translator`
  (`QObject.tr()` is Qt's own translation method, so assigning to `self.tr`
  shadows it and leaves the attribute untyped);
- PySide6 enums are accessed through their scoped enum
  (`Qt.ContextMenuPolicy.CustomContextMenu`, `QMessageBox.StandardButton.Yes`,
  `QStyle.StandardPixmap.SP_FileIcon`), which is the form the stubs declare;
- a value that PySide6 declares non-nullable but Qt can return as `None`
  (`QMenu.exec()`, `QListWidget.currentItem()`, `QAbstractItemView.model()`,
  `QApplication.style()`, `QStandardItemModel.item()`,
  `QGuiApplication.primaryScreen()`) passes through `gui.qt_optionals.maybe_none`
  so its `None` guard stays instead of being flagged as dead code;
- model overrides that accept the `QModelIndex | QPersistentModelIndex` union the
  base class declares normalise it once with `_transient_index`, because Qt itself
  always delivers a `QModelIndex`.

## Canonical checks

```text
ruff check .
pyright
python -m unittest discover -s test -p "test_architecture.py" -v
python -m unittest discover -s test -p "test_queue_completion.py" -v
python -m unittest discover -s test -p "test_gui_presentation.py" -v
python -m unittest discover -s test -p "test_*.py" -v
```
