# Smart capability guide

`core.smart` owns VMAF-guided analysis, sampling, measurement identity and
constraint decisions. CLI and GUI use the package API in `core/smart/__init__.py`;
other core packages import the concrete owner module. The default path lives in
`core/smart/v1/` and the experimental shot-based path in `core/smart/v2/`.

## Internal boundaries

- `v1.sampling.complexity` builds and parses Scout metadata only.
- `v1.sampling.planner` is deterministic and subprocess-free.
- `v1.sampling.scout` executes Scout and scene-alignment commands.
- `v1.bitrate` owns budgets, candidate search and reselection.
- `v1.cache` owns measurement/quality fingerprints and receipt construction.
- `v1.measurement` owns FFmpeg/VMAF execution for one candidate.
- `v1.session` owns one analysis call's references, candidate counters, measurement
  callbacks and backend fallback state. Each call has its own session.
- `v1.search` owns coarse/exact search, size calibration, adaptive expansion,
  holdout refinement and ambiguity checks. Its result carries candidates,
  selection, terminal failure and the window history needed for receipts.
- `v1.workflow` owns validation, reuse, sampling setup, temporary/log resource
  lifetime, stage calls and receipt persistence. It never imports `v1.decisions`.
- `v1.decisions` owns user choice policy and preserved size-miss actions, and is
  the only v1 owner that crosses to v2: it dispatches a `SegmentedAnalysisResult`
  to `v2.workflow.reselect` through a lazy import.
- `v2.optimizer` owns deterministic shot sampling and bounded discrete Pareto
  allocation. `v2.runtime` owns cancellable media measurements; `v2.receipts`
  owns its independent, versioned cache; `v2.workflow` owns shot detection,
  production-setting candidate search and holdout verification. These owners
  do not import v1 orchestration or Encoding; they may reuse only `v1.bitrate`,
  `v1.cache`, `v1.measurement` and `v1.vmaf`, and a v1 result is never treated as
  a v2 result.

Dependencies flow from `v1.workflow` to `v1.search` to `v1.session` to
measurement/runtime; lower owners never import orchestration, decisions or the
package facade. `test/test_architecture.py` checks the v1 direction, the v2
allow-list above and the facade's exported operations.
The package API exports application operations only. Fingerprint builders,
search utilities, measurement types and concurrency resources stay with their
concrete owners; there is no `core.smart_quality` compatibility facade.

Measurement identity includes the source, FFmpeg, bound encoder, measurement
settings and sample scheme. Quality and size policy changes may reuse measured
candidates; encoder or measurement changes may not.
V2 receipts additionally record continuous frame coordinates and the exact
sample/holdout topology. Decoder configuration and decode settings belong to
measurement identity. A failed bounded search is not proof of infeasibility.

Run Smart checks with:

```text
python -m unittest discover -s test -p "test_smart_quality.py" -v
python -m unittest discover -s test -p "test_analysis_runtime.py" -v
python -m unittest discover -s test -p "test_constraint_decisions.py" -v
python -m unittest discover -s test -p "test_smart_v2.py" -v
python -m unittest discover -s test -p "test_architecture.py" -v
```
