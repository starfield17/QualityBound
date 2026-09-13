# QualityBound algorithm

QualityBound searches for an encoding point under two explicit constraints: a
minimum perceptual-quality score and a maximum output size. Smart mode uses
sample encodes to reduce search cost, then validates its selection on different
content before performing the full encode.

## Scout and window selection

Short videos are analyzed whole. For longer sources, Scout performs a
low-resolution pass across the timeline and records spatial information (SI),
temporal information (TI), and scene boundaries. The planner selects a mixture
of difficult windows and time-stratified windows.

Fast, Balance, and Precise profiles increase Scout coverage, search-window and
holdout budgets, and search precision. Window identity uses actual timeline
coordinates and measurement settings, never a window's position in a list.

Search windows and holdout windows do not overlap. Just above the whole-video
threshold, the planner preserves the holdout budget and reduces the number of
search windows only when both sets cannot fit on the timeline.

## Candidate search

Each candidate is encoded and scored with Netflix VMAF v1.0.16. QualityBound
selects the normal/HFR 1080p or 4K model from source geometry and frame rate;
HFR starts at 50 fps. Reference and distorted samples are bicubic
fit-and-padded to the model canvas and normalized to 10-bit `yuv420p10le` at
the scoring boundary. This normalization does not change the production output
pixel format.

The reported candidate score is the lowest mean VMAF among measured windows,
not a whole-video average or the lowest individual-frame score. VMAF extraction
is CPU-only; source decoding and candidate encoding may independently use a
supported hardware path.

Candidate size prediction uses the largest measured encoded-sample bitrate,
including the container safety factor, plus the audio budget. A requested video
bitrate is not treated as an observed size.

## Independent holdout verification

After search selects a bitrate, QualityBound measures Scout windows that did
not participate in the search. If any holdout fails, all failed holdouts are
promoted into the search constraints and exact search resumes upward from the
current bitrate.

The profile's VMAF margin is a preference when size allows it. Meeting the
configured minimum VMAF remains valid even when there is no room for the
additional margin.

## Conflicts and final publication

When predicted quality and size constraints cannot both be satisfied, the
configured Smart policy decides whether to relax a constraint, skip, or request
a user decision. A Smart size miss is not success.

The full encode is written to a temporary file beside its intended target. The
temporary file is published only after validation. If the actual completed size
exceeds the limit, the file is preserved as `*.size-miss-<id>.*` for an explicit
accept, corrected-bitrate retry, or delete decision. A retry invalidates the old
selection and repeats search under a lower video-bitrate ceiling.

## Measurement reuse

Measurements are stored as versioned JSON receipts under
`workdir/analysis/receipts/`. Receipt identity includes the source, FFmpeg
binary, bound encoder, measurement settings, and sample scheme. Quality, size,
audio, or bitrate policy changes can re-evaluate existing candidates locally;
encoder or measurement changes cannot.

This process bounds the evidence QualityBound actually measured. It does not
turn unmeasured parts of a video into a statistical confidence interval. See
[Smart evaluation](smart-evaluation.md) for the current validation scope and
known limitations.
