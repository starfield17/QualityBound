# Queue owner

- `gui.queue.state` — Qt-free queue record/status/metrics logic. Imports `core` only.
- `gui.queue.actions` — Qt-free quality/size decision and file-side-effect actions
  over one queue record.
- `gui.queue.model` — `QueueTableModel`, `QueueColumn` and column metadata, cell
  formatting, roles and Qt notifications. Delegates mutations to `queue_state` and
  `queue_actions`; never owns receipt, constraint or file side effects.
- `gui.queue.view` — `ResponsiveQueueTableView`, header resize modes, reflow, and the
  `create_queue_view()` factory. May use `gui.queue.model`'s column definitions.
- `gui.queue.manager` — Qt worker/thread orchestration over the model.
- `gui.queue.workers` — the one-shot threads `MainWindow` starts directly:
  `QueueIntakeWorker` discovers/probes sources without encoding parameters;
  `QueuePrepareWorker` binds a batch at Start Queue. Capability detection lives
  separately in `gui.options.capability_worker`.
- New source records are `AWAITING_START`, with a source draft and no execution
  plan or tool snapshot. Start freezes current options and output directory,
  independently preferring per-item overrides. The model commits prepared plans
  atomically; preparation locks queue edits, reordering and decision activation.
  Resume retains its bound snapshot. Manual retry returns to source-draft state.
- `gui.queue.completion` — skipped-source publishing, reports, notifications and
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
- `gui.queue.window` owns the detached table; `gui.queue.item_details_panel`
  displays the selected record and emits actions without mutating it or starting
  work.

```text
python -m unittest discover -s test -p "test_queue_*.py" -v
python -m unittest discover -s test -p "test_constraint_decisions.py" -v
python -m unittest discover -s test -p "test_architecture.py" -v
```

Smart control coverage is in `test/test_smart_gui.py`; execution/publication
safety is in `test/test_smart_execution.py`.
