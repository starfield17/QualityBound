# QualityBound architecture

QualityBound separates platform-neutral compression behavior from its CLI and
PySide6 GUI. `main.py` is the composition root.

```text
main.py
├── cli/       command-line entrypoint and presentation
├── gui/       PySide6 entrypoint and presentation
│   ├── queue/       records, decisions, model/view, workers and completion
│   ├── comparison/  independent read-only A/B inspection and playback
│   └── options/     encoding controls, settings, presets and capabilities
└── core/
    ├── config/    paths, settings, and presets
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

`core.i18n` defines immutable multilingual `Text` values (`en`, `zh_cn`, `ja`), the
supported language names and the per-consumer `Translator`. CLI and GUI
declarations live beside their presentation code. Existing functional owners export shared text when
both consumers express the same concept; there is no catalog or registration.

The recursive architecture test enforces the root allowlist, capability-package
DAG, public adapters, and layer boundaries.

## Task entrypoints

GUI entry/composition stays in `gui/gui_entry.py` and `gui/gui_mainwindow.py`.
MainWindow groups its methods under searchable task comments; its method bodies
and instance ownership are unchanged by the directory arrangement. GUI packages
have empty initializers and callers import the concrete owner.

| Task | Entry and implementation | Contract to inspect | Focused tests under `test/` |
| --- | --- | --- | --- |
| Stop the application and release Qt objects | `gui/gui_entry.py:run_gui`; `MainWindow.shutdown`; queue manager and completion handler `shutdown` | Cancel and join workers before destroying their Qt owners; repeated shutdown is safe | `test_gui_lifecycle.py` |
| Start or resume a queue | MainWindow `_start_queue`; `gui/queue/workers.py:QueuePrepareWorker`; `gui/queue/manager.py:QueueManager.start` | `gui/queue/state.py:QueueItemRecord`, `core.models.EncodeRequest` | `test_queue_deferred_binding.py`, `test_queue_run_completion.py` |
| Resolve a quality/size decision | MainWindow `Constraint decisions` section; `gui/queue/model.py` delegates to `gui/queue/actions.py` | `core.models.DecisionOption`, `core.smart` decision operations | `test_constraint_decisions.py`, `test_smart_execution.py` |
| Inspect or seek comparison playback | `gui/comparison/window.py:ComparisonWindow.seek`; follow `state.py`, `decode.py` or `playback.py` for the affected operation | `decode.py:ComparisonMedia`, `state.py:ComparisonTimeline`; SPEC N11 | `test_video_comparison.py` |
| Change encoding controls or capabilities | `gui/options/encode_options_panel.py:EncodeOptionsPanel`; `capability_worker.py:EncoderCapabilityDetectWorker` | Panel contract in `gui/options/AGENTS.md`, `core.models.EncodeOptions` | `test_smart_gui.py`, `test_gui_workers.py`, `test_gui_presentation.py` |
| Change Smart v1 measurement | `core/smart/v1/measurement.py:score_candidate`; session owns references and resource state | `core.models.QualityCandidateResult`, runtime execution plans; SPEC F2/F3 | `test_smart_measurement.py`, `test_smart_measurement_cache.py`, `test_smart_session.py` |
| Change Smart v1 search/holdout | `core/smart/v1/search.py:run_search`; workflow owns setup and receipts | `core.models.QualitySearchResult`, `sampling/planner.py:SamplePlan`; SPEC F1/F2 | `test_smart_search.py`, `test_smart_workflow.py` |

Run a listed test with `python -m unittest discover -s test -p "<filename>" -v`.
`test_smart_budget.py`, `test_smart_configuration.py`, `test_smart_parallel.py`,
`test_smart_runtime.py` and `test_smart_receipts.py` cover the remaining v1
owners. Shared factories live in `_smart_quality_fixtures.py` and
`_analysis_runtime_fixtures.py`; these import neither Qt nor test cases.
Experimental v2 retains `test_smart_v2.py` and its existing owner map.

For a local change, locate the symbol before opening the file, for example:

```text
rg --files gui/comparison
rg -n 'def seek|def _request_frame' gui/comparison/window.py
rg -n 'def _refine_holdouts|def run_search' core/smart/v1/search.py
```

Expand to callers, dependencies or shared factories when a concrete question
needs them. The table identifies starting points, not complete impact sets.

## GUI presentation

`gui/theme.py` owns palette-aware semantic colour tokens and the single
application-wide stylesheet; `gui/icons.py` renders the toolbar line-icon set.
`gui/notification_banner.py` and `gui/queue/item_details_panel.py` are view-only
widgets that display queue state and emit user actions. `MainWindow` remains the
composition root: it owns the menu, shortcuts, tray menu, window geometry and the
collapsible queue-first layout. Presentation widgets never mutate queue records
or start encoding work.

`run_gui` owns final application cleanup even when the event loop returns early
or raises. MainWindow coordinates cancellation and waits for its workers and the
queue owners to finish before the entrypoint schedules window deletion and
processes deferred Qt deletion. Setup containers are constructed before their
children so Python garbage collection does not reverse their native ownership
order.

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
