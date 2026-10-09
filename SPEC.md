# QualityBound

## Soul

<!-- owned by the user; agents propose, never edit -->

Is:      A local batch compressor that turns an explicit quality and size
         constraint into one published file whose quality and size were measured
         on the finished output, run by the person who owns the machine and the
         media. Consumer intent.
Is not:  A general-purpose FFmpeg frontend — the three presets and the constraint
         search define the exposed options; anything else is refused, not added.
         A network service — probes, encodes, measurements and receipts stay in
         the operator's own process tree and files; no account, upload, telemetry
         or update server.
         A plugin or automation platform — the CLI exposes the same operations as
         the GUI, and nothing here is a third-party API.
S1 Measured over predicted — rejects: reporting a VMAF or size that came from the
   search samples, the size model or the requested bitrate instead of the finished
   file; validating on the windows the search used.
S2 A missed constraint is the operator's decision, not a result — rejects:
   publishing the closest oversize file over the requested target; reporting a size
   miss as success; silently skipping an item that failed its constraint.
S3 Refused with the reason over made to succeed — rejects: dropping the stream,
   codec or container part that fails so the run still produces a file; switching
   backend, algorithm or container without saying so; trusting an encoder name
   instead of probing it.
Status:  building

## Spirit

Done when:

1. A source larger than its constraint is run from the GUI (one queue click) or
   the CLI, and the published file's measured VMAF meets the target and its size
   meets the ratio — measured after the encode.
2. A run that would miss either constraint publishes nothing over the target and
   leaves the item for an explicit accept, retry or delete decision.
3. A source the tool cannot handle is refused with the reason, before it can
   produce a file that quietly lost part of the input.

Delivery:  a native package per platform (Windows x86-64/ARM64, Linux
           x86-64/ARM64, macOS arm64) containing a compatible FFmpeg and FFprobe,
           launched by double-click; `python main.py --cli` performs the same
           operations for batch use. The macOS package is ad-hoc signed, not
           notarized.

D1 Sample and measure windows instead of whole-file trial encodes ← S1, T4 —
   repeated full-file trials answer both questions at the cost this tool exists to
   avoid.
D2 Smart v1 stays the default while v2 is experimental ← S1 — v2 is explicit
   opt-in (`--smart-algorithm v2_experimental`).
D3 Smart measurements are cached as receipts keyed by source, FFmpeg, bound
   encoder, measurement settings and sample scheme ← S1 — a quality or size policy
   change may reuse measured candidates; nothing else may.
D4 Capability and receipt caches are local files under the app data directory ←
   Is not (network service), T4.

Open:

- N9 is decided but not implemented, so its `review:` has no failing test behind
  it yet: planning still does not probe subtitle codecs, and the failure surfaces
  after the full video encode. `docs/development.md` (Known limitations) carries
  the reproduction and tells users to select MKV until it lands.

## Do not build

- N1 No account, telemetry, update check or call to an endpoint this project
  operates. ← Is not, T4 — review: no network import anywhere in `core`, `cli` or
  `gui`, and none added by the diff.
- N2 Compression decisions live in `core`; the CLI and GUI are presentation and
  call the same operations. ← S1 — check:
  `python -m unittest discover -s test -p "test_architecture.py"`.
- N3 A file reaches the requested path only from a validated temporary file;
  nothing unvalidated overwrites it. ← S2 — check:
  `python -m unittest discover -s test -p "test_core_robustness.py" -k successful_encode_is_published_from_a_temporary_path`.
- N4 A size miss stays `NEEDS_DECISION` with the preserved file kept aside. ← S2 —
  check:
  `python -m unittest discover -s test -p "test_smart_v2.py" -k size_miss_preserves_file`.
- N5 Smart algorithm selection is explicit: v2 does not fall back to v1, and an
  unsupported timeline is rejected rather than resampled. ← S3 — check:
  `python -m unittest discover -s test -p "test_smart_v2.py" -k vfr_is_rejected_without_resampling`.
- N6 No third-party plugin or extension API. ← Is not — review: no entry-point
  registry and no `importlib` discovery of modules outside this repository.
- N7 No user-visible setting has to be hand-edited to complete Done-when. ←
  Delivery, T4 — review: a diff that makes Done-when require editing a file
  outside the application.
- N8 No FFmpeg option is exposed merely because FFmpeg supports it. ← Is not —
  review: a new widget or CLI flag that no preset and no constraint decision
  consumes.
- N9 A source whose subtitle codec the target container cannot carry is refused in
  planning, with the reason, before any encode runs. ← S3 — review: the planning
  path refuses instead of mapping the stream (check not yet written, see Open).
- N10 When something missing or broken turns up outside this spec, append it to
  "Found · Not doing" and keep going. Do not implement it.

## Frame

Discernment ran: a perceptual-quality claim needs the full file, and a full-file
trial encode is the cost being avoided. The frame is sample → measure → search →
validate on a disjoint holdout.

- F1 Search windows and validation windows are disjoint. ← S1 — check:
  `python -m unittest discover -s test -p "test_smart_v2.py" -k nonoverlapping_holdout`
  and `python -m unittest discover -s test -p "test_smart_quality.py" -k non_overlapping_windows`.
- F2 Measurement identity binds source, FFmpeg, bound encoder, measurement
  settings and sample scheme. ← S1, S3 — check:
  `python -m unittest discover -s test -p "test_smart_v2.py" -k measurement_identity_excludes_policy`
  and `python -m unittest discover -s test -p "test_smart_quality.py" -k analysis_from_a_different_encoder_is_never_published`.
- F3 Measurements are cached by window coordinate and measurement settings, never
  by a window's position in a list. ← S1 — check:
  `python -m unittest discover -s test -p "test_smart_measurement_cache.py" -k partial_rejection_is_cached`.
- F4 Every item is bound to one concrete encoder before analysis, and parallel
  workers receive deep copies. ← S2 — check:
  `python -m unittest discover -s test -p "test_parallel_transcoding.py" -k workers_preserve_bound_encoders`.

## Prior art

- Not used: general FFmpeg frontends expose encoder parameters and leave the
  quality claim to the encoder setting. This one makes a single measured
  constraint claim and refuses what it cannot verify.
- Reusing: FFmpeg and FFprobe are bundled per release, and Netflix VMAF is the
  measurement. Neither is reimplemented.

## Found · Not doing

## Amendments
