# Smart evaluation

Every measurement below was taken on 2026-09-09 on the v3.0.1 line, before the
deferred queue binding and container/subtitle work. It describes that revision,
not the current tree; the validation evidence is a single development case, not a
calibrated corpus, and no profile budget was tuned against it.

## Implemented scope

Smart retains ABR search, its VMAF model and its temporal quality gate. This
change adds no GPU VMAF or hardware decoding path.

- Reference files are identified by source, window coordinates and decoder,
  preventing holdout extraction from overwriting a search reference.
- A session reuses successful window measurements only with matching media,
  encoder and measurement settings. Explicit ambiguity remeasurement bypasses
  that cache. Failed or cancelled measurements are not inserted.
- Scout positions use seeded stratified jitter. Search reserves timeline
  coverage slots before difficulty representatives. Scene alignment preserves
  coverage and non-overlap.
- A validation window is reserved before Scout and excluded from its probes.
  If it fails and enters search, a fresh unobserved interval replaces it.
  Exhausted validation space cannot produce a confident success.
- Scheme and algorithm versions invalidate incompatible receipts.

The unscouted window is an additional check, not a statistical confidence
interval. Deterministic difficulty selection does not have known inclusion
probabilities; inverse-probability weighting cannot be inferred from ranks.
Risk weights, short-window ABR bias and GOP initialization remain research
questions. No weights were fitted to this corpus.

## Running a case

Generate sources using [smart-corpus.md](smart-corpus.md). Tools and fonts must
be explicitly supplied. Generated manifests may record resolved local paths
for reproducibility; keep these runtime artifacts out of committed documents.

```sh
python scripts/run_smart_case.py \
  --source /path/to/source.mkv \
  --ffmpeg /path/to/bin/ffmpeg \
  --ffprobe /path/to/bin/ffprobe \
  --workdir /path/to/empty-case-directory \
  --result /path/to/results/case.json \
  --profile balance --encoder libx265 --preset fast
```

The source must be outside the empty working directory. The runner forces
software decoding and CPU VMAF, validates CFR across the source timeline,
encodes the selected bitrate over the full source, and measures every frame.
It aligns validated CFR inputs by frame index to avoid cross-container
timestamp rounding pairing adjacent frames. Truncated frame counts fail.
VFR requires a separately documented CFR derivative and is rejected directly.

Optional controls:

- `--max-video-kbps`: explicit search ceiling, useful for lossless inputs.
- `--oracle`: bounded full-file bitrate grid with local refinement; reports a
  tested bracket, not a mathematically exact optimum.
- `--disable-window-cache`: measure the same planner without measurement reuse.
- `--implementation-root`: compare against a separate checkout using the same
  runner and tools. Record any corrections made to the baseline.

Run performance comparisons sequentially on an otherwise idle machine. Repeat
in alternating order and report distributions. Encoding process counts include
both passes of two-pass encoding; they are not unique-window counts. Wall time
from simultaneous corpus generation is unsuitable for speed comparisons.

## Preventing corpus overfitting

Keep development, calibration and acceptance groups separate. Freeze recipes,
seeds, tool identity and decision rules before opening acceptance results.
Different seeds alone do not make independent content families. Split real
footage by original source/title, keeping derivatives in the same group.

Synthetic recipes exercise failure modes but do not reproduce the distribution
of real footage. Include independently sourced live action, animation, screen
text, noise, dark gradients, motion and transitions. Preserve black frames and
credits when evaluating full-file size. Record provenance and licenses.

Report quality false passes, size false blocks, bitrate regret, prediction error
and analysis cost per family and overall. Compare profiles and leave-one-family
out results; report missing oracle/ground-truth observations explicitly. Avoid
claiming a zero failure rate from a small corpus. Repeatedly tuning to acceptance
results turns that set into development data and requires a new locked set.

## Validation evidence

Local validation rendered six development recipes at their default geometry and
duration, and exercised a 640x360 eight-second smoke clip. The latter validates
execution only. A contact sheet verified changing visual content in the first
normal-length texture recipe; this is not visual acceptance of every recipe.

On that normal-length 1080p recipe, Balance at a 90-point target with a 5 Mbps
ceiling and x265 ultrafast selected 5 Mbps with and without window caching.
Both full-file checks scored 92.5795. Caching reduced candidate encode and VMAF
executions from 19 to 16, with three measurement hits and three reference hits.
An isolated repeat took 33.17 seconds with caching versus 39.39 seconds without
it (15.8% less analysis time); the repeat full-file score was 92.5743. These are
single observations, with minor encoder run-to-run variation, not a speedup
distribution or a bitwise determinism guarantee.
This single development case does not establish a general speedup or accuracy
improvement. The full locked corpus, real-source quality study, profile
comparison and oracle sweep have not yet been completed.

A runner defect was caught during smoke validation: differing Matroska and MP4
time bases initially yielded a spurious score near 59.3. CFR frame alignment
restored approximately 90.58, matching the Smart measurement near 90.59. The
runner includes regression checks for alignment and incomplete frame coverage.
For sources longer than two minutes, validation uses sequential 30-second VMAF
segments with one-second overlap. Overlap frames are removed before global mean
and rolling worst-one-second pooling, bounding memory without hiding boundary
intervals.

Repository validation passed 542 tests at that revision, including the explicitly
configured FFmpeg integration test, plus Ruff, Pyright, compileall and generated
icon verification. Native Windows/Linux packaging was not run locally. The count
is a snapshot of that revision; the current suite is a different size.
