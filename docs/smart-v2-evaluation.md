# Smart v2 evaluation and development evidence

The checked-in [development measurements](smart-v2-development-results.json)
are mechanism evidence from macOS arm64 on 2026-10-08. They are not a calibrated
acceptance corpus or an estimate of real-world savings. No real source corpus
was supplied. Default algorithm and profile budgets were not tuned against
these results. Hardware/platform CI routing is unchanged.

## Reproduce with explicit tools

```text
python scripts/generate_smart_v2_corpus.py \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe \
  --output-dir workdir/v2-development --seed 1729 --split development

python scripts/run_smart_v2_case.py \
  --source workdir/v2-development/high-low-high.mkv \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe \
  --workdir workdir/v2-case --result workdir/v2-case.json \
  --backend cpu --codec hevc --preset ultrafast --profile balance \
  --target 80 --max-output-ratio 1 --max-video-kbps 2000 \
  --source-group synthetic-recipe-high-low-high --split development \
  --compare --baseline-trials 9

python scripts/verify_smart_v2_frames.py \
  --source workdir/v2-development/high-low-high.mkv \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe \
  --workdir workdir/v2-frame-check
```

The generator imports no Smart algorithm. It records its seed, recipes,
frame ranges, tool versions, file hashes and provenance groups. Cases cover
high/low/high complexity, one long shot, rapid cuts, a one-frame flash, a fade,
a short difficult event, and continuous sine audio with subtitles and chapters.
Lossless sources use FFV1, SDR BT.709, 320×180, 24fps by default. The explicit
post-concat frame clock ensures even a one-frame clip has the correct duration.

Synthetic `source_group` is the recipe lineage, independent of split and seed.
Changing a split label or random seed cannot make that same recipe family into
independent development and acceptance sources. Synthetic sets remain
mechanism-only. Use separate source groups/recipes for development, calibration
and acceptance, and disjoint origin groups for real content. Do not inspect
acceptance results to tune search or prediction parameters.

The frame oracle slices and concatenates with the actual FFmpeg command
builders and a lossless test encoder, then compares independent decoded
`framemd5` hashes against the complete source. It deliberately includes cuts
inside moving content and one-frame segments. 192-frame 24fps, 90-frame
30000/1001fps, and a 192-frame source beginning at 10s all preserved exact frame
identity and order. Production lossy outputs separately undergo complete
frame/cadence, stream/timeline and quality checks.

## Measured comparison

One eight-second high/low/high fixture used mean target80, shot mean floor76,
temporal floor72, minimum250kbps and ceiling2Mbps, HEVC ultrafast, AAC128k,
Balance and a cold cache. All four listed outputs passed the same complete
quality gates. The size ceiling was100% of the lossless source, so it did not
force their relative ordering. The whole-file baselines measured the minimum
allowed rate, a coarse grid and failed/passing bracket refinements. They chose
the smallest actually measured passing output, not an interpolated bitrate.

| Method | Bytes | Complete mean | Worst1s | Native/search wall seconds | Child CPU seconds |
| --- | ---: | ---: | ---: | ---: | ---: |
| Smart v1 |403,285|91.181|78.243|5.89|41.33|
| Smart v2 |349,961|88.859|73.392|9.72|56.17|
| Whole ABR,9 measured points |336,987|88.585|73.327|12.84|94.43|
| Whole two-pass,4 measured points |379,964|90.489|79.319|6.50|44.62|

V2 was13.2% smaller than v1 and7.9% smaller than the measured two-pass output,
but3.9% larger than the measured whole ABR output. Its complete mean and local
minimum also differ from the other outputs. These results support neither a
universal size improvement nor equal subjective quality. The whole ABR search
and trellis are both finite searches; neither establishes a global optimum.

V2 performed16 segment encode invocations including preflight, trials and
holdouts/final execution. Its initial predicted size346,530bytes missed the
349,961-byte result by+0.99%. In this case it took about1.65times the native v1
wall time. Baseline costs include their complete-scoring search sweeps; v1's
native cost excludes the evaluation runner's later complete quality measurement
(current runner reports that cost separately). These are single-run,
uncalibrated costs; CPU seconds aggregate child processes and are not wall time.
GPU time is unavailable and recorded as null. A warm receipt run should never
be represented as a cold-run performance result.

## Structural and failure evidence

With complete decoder-header compatibility checks enabled:

- Software HEVC passed high/low/high, rapid cuts (12 retained shots), the
  one-frame flash, auxiliary streams, nonzero source origin and two-pass shots.
  Long-shot sampling/holdout, fade and brief-event fixtures were also exercised.
- Software AV1 passed using an explicitly supplied FFmpeg9.0.2 build with
  dav1d. The FFmpeg9.0.1 project bundle tested here lacked a working software
  AV1 decoder on this host and explicitly failed preflight. The application
  never substituted the second tool path automatically. The bound SVT backend
  declares two-pass unsupported in existing capability metadata.
- VideoToolbox was actually tested. Its bitrate-dependent decoder parameter
  sets did not satisfy the uniform configuration check, so v2 returned
  `UNSUPPORTED`. An earlier decode-only success was superseded by this stronger
  check. NVENC, QSV and AMF were not available for native tests here; the shared
  runtime gate does not make them verified backends.
- A real completed HEVC output of349,961bytes against a347,000-byte limit
  returned `NEEDS_DECISION`, retained its encoded file and left an existing
  destination intact. Quality had already passed before this actual-size gate.
- Unit regression checks cover cross-boundary degradation, local repair and
  rescoring, exhausted budgets, cancellation of silent subprocesses, temp-file
  cleanup, publication failure, source/parameter identity, old presets/defaults,
  GUI/CLI semantics, retry/relax decisions and invalid receipts.

The initial v2 development gate passed Ruff, strict Pyright, architecture checks
and576 complete unit tests
on this host (one existing platform-specific skip). The full suite used a
temporary clean app configuration and restored the user's configuration:
one pre-existing smoke test otherwise assumes the default workdir despite the
local saved workdir pointing at an old checkout. No test or CI gate was weakened.
Cross-platform packaging/CI jobs and unavailable hardware were not run locally.

## Remaining experimental questions

Measure real, provenance-separated sources before making performance claims:
size at the same measured mean/local gates; prediction error distribution;
short-shot ABR/GOP startup bias; rare-event misses and their repair cost;
scene threshold behavior on fades/flashes; state compression misses; long-video
memory/time; subtitle/container edge cases; and compatible configurations for
each native hardware backend. Compare against both v1 and whole-file ABR/two-pass
searches with the same bound encoder/preset and complete scoring. Keep failed,
unsupported and exhausted runs in the report rather than counting only wins.

## Official dependency upgrade verification (2026-10-08)

The published `ffmpeg-9.0.2-vmaf-v1.0.16-r1` release passed verification
contract v4 on all five native targets. The release records 24 decoded and 24
ordered finite VMAF-scored frames for HEVC and AV1 on every target, alongside
the existing four-model, Scout, normalization, architecture and linkage gates.
macOS builds official FFmpeg9.0.2 and static dav1d1.5.4; Windows/Linux mirror the
pinned BtbN 9.0.2-plus-22-commits recipe. See the distribution's published
[provenance and reports](https://github.com/starfield17/ffmpeg-vmaf-v1-builds/releases/tag/ffmpeg-9.0.2-vmaf-v1.0.16-r1).

Using the actual published macOS pair and the existing seed1729 development
fixture, Smart v2 completed both software HEVC (mean88.8591,349,961bytes) and
AV1 (mean89.3022,469,198bytes), with complete final quality checks. The independent
frame oracle retained all192 frames in order, including one-frame boundaries.
These checks establish operation with the new distribution; they do not isolate
FFmpeg version effects, compare codec efficiency, or establish real-content gains.
The bundled AV1 decode fix is the inclusion of dav1d: the old distribution's
AV1 roundtrip is rejected specifically during software decoding.

VideoToolbox still returned `UNSUPPORTED` because different bitrates produced
incompatible decoder parameter sets. Its runtime check remains unchanged; a
version upgrade did not resolve this case. Native GPU backends on other platforms
remain untested by this development fixture. Portable measurements and native
roundtrip summaries are recorded under `dependency_upgrade` in
[the development results](smart-v2-development-results.json); previous benchmark
results retain their original tools and measurements.

The dependency upgrade passed Ruff, strict Pyright, architecture checks and all577
unit tests locally. The existing optional normalization integration test was
enabled with the explicitly supplied official macOS FFmpeg pair; no tests were
skipped in that run. The clean-config fixture precaution described above was
retained, and the original local application configuration was restored.
