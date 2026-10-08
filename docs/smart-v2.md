# Smart v2 (experimental)

Smart v1 remains the factory default. Old options and presets without
`smart_algorithm` load as `v1`. V2 is selected explicitly in the GUI Smart
algorithm field or with `--smart-algorithm v2_experimental`. An explicit saved
v2 preset retains that selection. Fixed-bitrate mode does not use v2.

```text
python main.py --cli encode input.mkv --compression-mode smart \
  --smart-algorithm v2_experimental --backend cpu --codec hevc \
  --min-vmaf 90 --analysis-profile balance --max-video-kbps 20000 \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
```

The algorithm is in `core.smart.v2`; final execution and publication are in
`core.encoding.segmented`; FFmpeg commands are in `core.ffmpeg.segmented`.
V2 uses `SegmentedAnalysisResult`, not a v1 single-bitrate result. No encoder or
algorithm fallback occurs when v2 fails or is unsupported.

## Quality contract

For target T, the optimizer minimizes predicted file size while selecting one
actually measured candidate per shot. It requires duration-weighted whole-video
mean VMAF >= T, each shot mean >= max(0,T−4), and each shot's worst consecutive
one-second mean >= max(0,T−8). A shot shorter than one second uses its complete
mean for the temporal gate. CFR frame weights equal duration weights. The
one-second window has `ceil(fps)` frames, including for fractional CFR.

The final file is scored completely with the same existing source-selected
VMAF model and normalized scoring canvas. One set of frame scores supplies the
whole mean, every shot mean, every shot temporal gate and all one-second
windows crossing shot boundaries. This contract differs from the v1 sampled
window gate. Passing samples alone provides no statistical guarantee about
unmeasured content. VMAF is a metric, not a guarantee of subjective equivalence.

## Detection, search and allocation

1. Decode/probe the complete video timeline. Reject discontinuities and VFR;
   do not change cadence. Scan all frames at width at most480 using FFmpeg
   `scdet` threshold10. Keep continuous, nonoverlapping `[start,end)` frame
   ranges, including one-frame shots. Source bitrate does not define boundaries.
2. Choose disjoint samples in equal time strata and reserve a separate final
   stratum for holdout. If the search plus holdout cannot fit, measure the
   complete shot and retain its encoded candidate. Every trial uses the bound
   production encoder, preset, pixel format, VBV and two-pass selection.
3. Start from the global video budget, explore lower/higher rates and refine
   near the quality target within the profile's candidate budget. Confirm a
   nonmonotonic curve by remeasuring a point near the target. Requested rate is
   not observed rate: use actual video packet bytes and measured frame scores.
4. Remove dominated feasible candidates and propagate a Pareto frontier of
   cumulative video bytes and frame-weighted quality. Above the state budget,
   bin cumulative quality and retain each bin's cheapest state plus endpoints.
   Linked states recover real candidate combinations. Final constraints use
   their unrounded measured values. Record state compression, including on
   failed searches; neither success nor failure claims global optimality.
5. Verify selected untested holdouts. Add their observations and reselect;
   newly selected candidates need their own holdout. If bounded search cannot
   satisfy quality, add at most one higher-rate point per affected shot per
   rescue round and verify the new selections. Exhaustion remains unsatisfied.

Sample size estimates use measured video bytes per frame, scaled to the shot
with an initial10% allowance. Holdout bytes can raise that estimate. A complete
candidate uses its actual video bytes. Audio is budgeted once for the complete
timeline;2% of the file ceiling is reserved for container overhead. These
margins and search parameters are initial experimental choices, not calibrated
probabilities or guarantees. Actual final size remains the publication gate.

| Profile | Search windows/shot | Window length | Initial points/shot | Frontier states | Rescue/final repair rounds |
| --- | ---: | ---: | ---: | ---: | ---: |
| Fast |1|2s|3|2,048|1|
| Balance (default) |2|2s|5|8,192|2|
| Precise |3|3s|7|16,384|3|

Each sampleable shot has one further holdout of the same length. The log and
GUI queue tooltip show shot count and initial sample-encode budget before
candidate measurement. Holdouts, a possible nonmonotonic confirmation,
preflight, rescue and final repair are additional costs. Every rescue/repair
round adds at most one point per affected shot. A large number of short shots
requires complete trial encodes and may be expensive even in Fast mode.

## Execution, compatibility and publication

The first version accepts CFR SDR. HDR transfer functions currently excluded
by Smart, VFR and unmeasurable/discontinuous timelines are explicitly rejected.
The tool must be able to decode both the source and the chosen codec. Presence
of an AV1 encoder alone is insufficient; a build without a working AV1 decoder
fails preflight. Explicit tools are honored throughout, including FFprobe.

Preflight actually encodes two short segments at different rates, compares
decoder configuration, concatenates by stream copy and completely decodes the
result. Every final shot is independently decoded and must start with a key
frame. All selected shots must have matching codec/profile/level, geometry,
pixel format, aspect, cadence/timebase, colors and decoder headers. HEVC checks
VPS/SPS/PPS, excluding bitrate-dependent encoder information SEI; AV1 checks
its sequence configuration. x265 uses a common Main-tier level for the candidate
envelope and closed GOPs. Its resolved level is part of measurement identity.
An excessive envelope requests an explicit bitrate ceiling instead of silently
clamping it. Hardware-native configurations that vary incompatibly by rate
are rejected, even if a software decoder can play the assembled file.

Segments run serially within the existing file worker. Exactly matching
complete candidates are reused only after their stored digest is checked.
Concatenation offsets use frame-derived duration, not rounded segment container
duration. Video is copied into the final container; audio is processed once
continuously from the original source. Existing subtitle conversion/copy,
chapters, metadata and external subtitle behavior is preserved. Validate total
decoded frames, CFR cadence and zero video origin, stream counts, normalized
audio packet coverage and chapter coordinates/titles. Encoder delay and packet
duration determine the audio timestamp allowance.

After complete scoring, repair local/boundary violations first; if only the
mean fails, repair the largest duration-weighted deficit. Measure at most one
new point per affected shot per round, reoptimize and score the complete new
assembly. A failed final-quality gate is never success, even after the last
repair. Only structural and quality success reaches actual-size inspection.
An actual size miss preserves `*.size-miss-<id>.*` and returns `NEEDS_DECISION`.
Accept/delete use the existing decision mechanism; retry lowers the global
video-combination budget while retaining per-shot bitrate caps. Relaxing size
releases that retry cap and reselects measured candidates.

The successful output is published from a validated temporary path next to
its destination. Cancellation stops silent as well as logging subprocesses,
cleans unpublished temporary output and uncommitted candidate assets. Committed
receipt candidates remain reusable. Source/tool/settings identity is checked
again before publication. Publication failure preserves the previous target.

## Receipts and observable costs

Receipts are independent under `workdir/analysis/smart-v2/<fingerprint>/`, with
versioned schema/scheme, source/tool/bound-encoder/production settings, resolved
configuration, frame timeline and actual sample coordinates. The receipt stores
sample topology integrity, candidates and optional relative-path artifact
digests. Quality and size policy changes reselect candidates; changes to source,
tools, encoder, production/scoring settings, decode acceleration, profile or
resolved decoder configuration remeasure. V1 results and receipts are untouched.

Results expose per-shot candidates/selections, predictions, complete quality
metrics, approximation, cache hits, encode/VMAF invocation counts and wall time
by phase. The encode count includes preflight, trials, holdouts and final shots.
FFmpeg `-benchmark` supplies user+system CPU seconds by phase; FFprobe CPU is
not included in that field. The evaluation runner additionally records total
child-process CPU on supported platforms. GPU execution time is unavailable
and is recorded as null. Hardware wall time must not be called GPU time.
Full timeline/packet probes and frame-score arrays also consume memory; no
long-video memory/performance bound has been calibrated yet.

## References and evidence

[Netflix Dynamic Optimizer](https://netflixtechblog.com/dynamic-optimizer-a-perceptual-video-encoding-optimization-framework-e19f1e3a277f)
motivates shot candidate curves and combination search. V2 fixes resolution and
encoder and uses discrete measured points, so Netflix's published savings do
not apply to this implementation.
[VIF per-shot ladder research](https://arxiv.org/abs/2408.01932) explores predictive
candidate reduction; v2 does not train or use such a model.
[x265 zones](https://x265.readthedocs.io/en/master/cli.html#cmdoption-zones) remain
a future continuous-encoding comparison.
[FFmpeg concat](https://ffmpeg.org/ffmpeg-formats.html#concat) requires compatible
streams; decoding alone is not the entire compatibility check.

See [evaluation and development results](smart-v2-evaluation.md). Synthetic
fixtures prove mechanisms, not real-world quality or savings. Development,
calibration and acceptance sources must be disjoint by provenance group;
do not tune against acceptance results.
