# Encoding capability guide

`core.encoding` owns the transition from an immutable plan to analysis and
encoding results. CLI and GUI callers use the package API; implementation
modules import concrete owners in `config`, `media`, `ffmpeg`, and `smart`.

## Internal boundaries

- `planning` discovers inputs, resolves one encoder, probes media, and builds
  validated plan items.
- `analysis` runs Smart analysis and converts policy outcomes into terminal
  item results or encode-ready items.
- `process` owns FFmpeg process lifecycle, cancellation, logging, and progress
  parsing.
- `item_results` owns result construction, sidecar publication, failure logs,
  and preserved size-miss paths.
- `executor` performs serial item/plan execution and publishes validated Smart
  output.
- `segmented` owns experimental v2 shot execution, video-copy assembly,
  continuous auxiliary-stream muxing, complete final quality gates, bounded
  local repairs and validated publication. Command construction stays in
  `core.ffmpeg.segmented`; shot search stays in `core.smart.v2`.
- `parallel` deep-copies already-bound plan items, runs Smart analysis for the
  whole queue first, then dynamically schedules full-file encode workers.

Never share a mutable `EncodePlanItem` between workers and never rebind or
round-robin its encoder in the executor. A Smart output is
written beside its destination under a temporary name and published only after
size validation. A size miss remains a `NEEDS_DECISION` result and its preserved
file must not overwrite the requested output.
V2 checks complete frame coverage/cadence, decoder compatibility, audio/chapter
timelines and whole-video/local/boundary VMAF before the actual-size gate. Its
final scorer shares the analysis resource semaphore; shots remain serial
within each file. Unsupported streams/backends never silently switch to v1.

Helpers shared between modules of this package keep their leading underscore
because they are not public core API, and the providing module lists them in
`__all__` so the cross-module contract is checked rather than assumed
(`process.py`, `item_results.py`).

Run Encoding checks with:

```text
python -m unittest discover -s test -p "test_parallel_transcoding.py" -v
python -m unittest discover -s test -p "test_quality_tuning.py" -v
python -m unittest discover -s test -p "test_subprocess_utils.py" -v
python -m unittest discover -s test -p "test_smart_v2.py" -v
python -m unittest discover -s test -p "test_architecture.py" -v
```
