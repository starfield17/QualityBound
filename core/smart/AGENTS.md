# Smart capability guide

`core.smart` owns VMAF-guided analysis, sampling, measurement identity and
constraint decisions. CLI and GUI use the package API; other core packages
import the concrete owner module.

## Internal boundaries

- `sampling.complexity` builds and parses Scout metadata only.
- `sampling.planner` is deterministic and subprocess-free.
- `sampling.scout` executes Scout and scene-alignment commands.
- `bitrate` owns budgets, candidate search and reselection.
- `cache` owns measurement/quality fingerprints and receipt construction.
- `measurement` owns FFmpeg/VMAF execution for one candidate.
- `session` owns one analysis call's references, candidate counters, measurement
  callbacks and backend fallback state. Each call has its own session.
- `search` owns coarse/exact search, size calibration, adaptive expansion,
  holdout refinement and ambiguity checks. Its result carries candidates,
  selection, terminal failure and the window history needed for receipts.
- `workflow` owns validation, reuse, sampling setup, temporary/log resource
  lifetime, stage calls and receipt persistence. It never imports `decisions`.
- `decisions` owns user choice policy and preserved size-miss actions.
- `v2.optimizer` owns deterministic shot sampling and bounded discrete Pareto
  allocation. `v2.runtime` owns cancellable media measurements; `v2.receipts`
  owns its independent, versioned cache; `v2.workflow` owns shot detection,
  production-setting candidate search and holdout verification. These owners
  do not import v1 orchestration or Encoding. Shared measurement primitives may
  be reused without treating a v1 result as a v2 result.

Dependencies flow from `workflow` to `search` to `session` to measurement/runtime;
lower owners never import orchestration, decisions or the package facade.
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
