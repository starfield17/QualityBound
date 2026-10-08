# Synthetic SMART Video Corpus

This document specifies the architecture, recipe taxonomy, split isolation, and usage
of the deterministic synthetic SMART video corpus generator implemented in
`scripts/generate_smart_corpus.py`.

---

## 1. Overview & Objectives

The synthetic corpus generator provides mathematically reproducible, parameterized
video clips designed to benchmark and evaluate SMART video quality analysis, bitrate
prediction, scout sampling, and decision boundaries.

Key architectural properties:
- **Pure Standard Library & Direct Executables**: Implemented entirely with Python stdlib
  (plus optional Pillow development fallback for font rasterization). FFmpeg and FFprobe
  are invoked via explicit `argv` lists without a shell (`shell=False`).
- **No Algorithmic Coupling**: The generator has zero imports from `core.smart` sampling,
  risk, or evaluation modules. Recipe seeds and timeline placements are completely
  independent of SMART scout window positions and sampling grids.
- **FFV1 SDR Base Sources**: Master video clips are generated directly into Matroska (`.mkv`)
  containers using the lossless `ffv1` codec (level 3, multithreaded, BT.709 SDR color tags).
- **True 10-bit Gradient Synthesis**: 10-bit gradient ramps are evaluated directly in 10-bit
  precision ($Y \in [0, 1023]$) on native `yuv420p10le` sources before `geq`, avoiding 8-bit
  quantization or post-filter upsampling.
- **Dynamic Motion Overlays**: Dynamic elements (Lissajous orbits, orthogonal sweep lines,
  rotating crosshair reticles, high-velocity particle occlusions, and needle ticks) use
  per-frame evaluated overlays (`eval=frame`), ensuring verified pixel motion across frames
  on stationary backgrounds.
- **Bound Noise & Gradient Seeds**: Every `noise` filter explicitly binds `all_seed` and every
  `gradients` filter binds `seed` to the recipe's deterministic integer seed, ensuring true
  reproducibility and seeded parameter jitter.
- **Optional HEVC and H.264 Derivatives**:
  - `--hevc-derivative`: Encodes long-GOP HEVC using CPU `libx265` with `-x265-params keyint=fps*10:min-keyint=fps*10:scenecut=0`.
  - `--h264-derivative`: Preserves `libx264` option; fails closed with explicit exception and stderr if `libx264` is missing from the binary.
- **Audit-Ready Manifest**: Emits JSON manifests strictly conforming to `schema_version: 1`
  consumable by `scripts/evaluate_smart.py` and `core.smart.v1.evaluation.load_evaluation_manifest`.
  Commands use `scripts/run_smart_case.py` with `--workdir {case_dir}`. No fabricated measurements.

---

## 2. Split Taxonomy & Group Isolation

The corpus defines **42 canonical cases**:
- **36 standard cases**: 12 in `development`, 12 in `calibration`, 12 in `acceptance`.
  Durations range from 45.0 to 120.0 seconds.
- **6 extended cases**: 6 in `long`, each exactly 600.0 seconds (10 minutes).

### Group Isolation Contract

To prevent data leakage, calibration overfitting, and false optimism in quality validation:
1. **Disjoint Recipe Groups**: The base recipe groups for `development`, `calibration`,
   `acceptance`, and `long` are strictly pairwise disjoint sets (`set(g_a) & set(g_b) == ∅`).
   Each case possesses a unique group identifier.
2. **Held-Out Acceptance Combinations**: Acceptance is not merely a different random seed
   of development recipes. Acceptance deliberately holds out:
   - **Non-linear motion trajectories**: Lissajous 3:4 harmonic orbits, chaotic non-periodic
     acceleration with affine shear oscillation.
   - **Texture variants**: Multi-octave Sierpinski fractal textures, high-density micro-jittering
     matrix tables, true 16-level stepped near-black quantization banding with micro-dither,
     analog streak imperfections, non-stationary bursty noise.
   - **Compound transitions and events**: Rapid multi-frame strobe whip transitions, split-push
     dissolves with simultaneous luma desaturation, ultra-short 0.3s micro-transient multi-target
     occlusions, and dual compound bursts (luminance flare + high-entropy noise).
3. **Variant Consistency**: All derivatives (e.g. HEVC long-GOP) and resolution variants
   remain assigned to the exact same group and split.

---

## 3. The Six Scene Families

Every split distributes clips across six fundamental synthetic scene families:

| Scene Family | Visual Stimuli & Synthesis | Stress Characteristics for SMART |
| --- | --- | --- |
| `moving_textures` | Scrolling textures, harmonic plasma, Mandelbrot fractals, Lissajous orbits | High spatial & temporal entropy; stresses motion estimation and bitrate bounds. |
| `lines_and_text` | Scrolling tickers, fine orthogonal grids, crosshairs, telemetry HUDs | Sharp high-frequency edges; tests ringing, mosquito noise, and sub-pixel edge degradation. |
| `dark_gradients` | Native 10-bit linear ramps, radial vignettes, conic sweeps, 16-step quantization stairs | Very low IRE luminance; stresses quantization banding, bit depth (8 vs 10-bit), and dither retention. |
| `film_noise` | Temporal Gaussian noise, coarse luma grain, chroma flicker, seeded bursty grain | High temporal variance with low structural coherence; stresses spatial vs temporal filtering trade-offs. |
| `transitions` | Cross-dissolves, directional wipes, dip-to-black, strobe whips | Rapid scene energy change; stresses scout window placement and transition detection. |
| `short_difficult_events` | Center flashes, block glitches, high-speed corner particles, transient bursts | Highly localized 0.3s–2.0s events in static plates; tests sensitivity to missed transient degradations. |

---

## 4. Extended Long Cases (600 Seconds)

Six clips feature a 600.0-second baseline with a single subtle or sudden transient event:

| Case ID | Group | Event Time Window | Actual Stimulus Description |
| --- | --- | --- | --- |
| `smart-long-01-rare-statictick` | `long_rare_statictick` | ~184.0s (1.5s) | Static plate with single needle/tick dynamic movement |
| `smart-long-02-rare-subtledrift` | `long_rare_subtledrift` | ~421.0s (2.0s) | Dark linear gradient with transient luminance motion flare |
| `smart-long-03-rare-burstluma` | `long_rare_burstluma` | ~95.0s (1.8s) | Seeded uniform fine noise plate with single burst anomaly |
| `smart-long-04-rare-gradientband` | `long_rare_gradientband` | ~512.0s (2.5s) | Native 10-bit dark gradient with 4-level quantization step shift |
| `smart-long-05-rare-microtexture` | `long_rare_microtexture` | ~260.0s (1.2s) | Steady micro-texture with single high-energy contrast surge |
| `smart-long-06-rare-glitchburst` | `long_rare_glitchburst` | ~370.0s (1.0s) | Static geometric plate with single multi-band glitch burst |

**Design Rationale**: Sparse scout sampling algorithms can easily achieve high average VMAF
scores on long videos while completely failing to detect isolated brief quality dips.
These cases test whether SMART sampling strategies can discover or bound risk on rare transient anomalies.

---

## 5. Limitation: Real Footage Holdout Requirement

> [!IMPORTANT]
> **Synthetic video is a necessary stress-testing instrument, but it is NOT sufficient for overall encoder quality generalization.**
> Real-world camera footage must remain a separate, non-overlapping holdout corpus.

### Why Synthetic Video is Essential
1. **Mathematical Edge Cases**: Pure algorithmic generation produces extreme mathematical
   patterns (e.g. perfect orthogonal 1-pixel grids, stepped low-IRE gradients, non-stationary
   temporal noise) that cleanly isolate decoder/encoder failure modes without camera noise confounding the test.
2. **Determinism and Licensing**: 100% reproducible anywhere on CPU without proprietary media assets.

### Why Real Footage is Still Required for Generalization
1. **Optical Physics & Natural Sensor Noise**: Real cameras introduce Bayer debayering artifacts,
   natural photon shot noise, chromatic aberration, lens vignetting, rolling shutter skew, and non-linear dynamic range curves.
2. **Natural Scene Statistics**: Natural photographic content obeys power-law spatial frequency
   distributions ($1/f^\alpha$) and natural motion blur that synthetic procedural generators only approximate.
3. **Production Editing Cadence**: Human editing introduces subtle camera shakes, cuts, dissolves,
   variable framerates, telecine pulldowns, and complex lighting transitions.
4. **Conclusion**: An evaluation passed exclusively on synthetic footage demonstrates codec stability
   under synthetic stress, but production release criteria MUST mandate passing a separate holdout
   corpus composed of authentic real-world camera footage.

---

## 6. CLI Usage & Flags

The generator CLI supports manifest generation, rendering, smoke overrides, and format configuration:

```bash
python scripts/generate_smart_corpus.py [OPTIONS]
```

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--ffmpeg` | *None (required)* | Path to explicit FFmpeg executable. |
| `--ffprobe` | *None (required)* | Path to explicit FFprobe executable. |
| `--output-dir` | `workdir/smart-corpus` | Destination directory for manifest and video clips. |
| `--font-file` | *None (required)* | Path to explicit font file for text overlays (TTF/OTF/TTC). |
| `--split` | `all` | Split to generate: `development`, `calibration`, `acceptance`, `long`, or `all`. |
| `--limit` | `None` (all) | Limit the number of cases emitted/rendered. |
| `--render` | `False` (flag) | If omitted, writes recipe manifest only. When set, renders videos. |
| `--width` | `None` | Smoke test override for width in pixels. Flags cases as smoke. |
| `--height` | `None` | Smoke test override for height in pixels. Flags cases as smoke. |
| `--duration` | `None` | Smoke test override for clip duration in seconds. Flags cases as smoke. |
| `--hevc-derivative` | `False` (flag) | Also render optional long-GOP HEVC derivative via `libx265` (CPU). |
| `--h264-derivative` | `False` (flag) | Render optional long-GOP H.264 derivative via `libx264` (CPU). |
| `--pillow-text` | `False` (flag) | Force Pillow rasterized text overlay fallback instead of `drawtext`. |

### Smoke Overrides Policy

When `--width`, `--height`, or `--duration` are passed:
- In the manifest metadata: `"smoke": true`, `"acceptance_valid": false`.
- Event timestamps are scaled proportionally so that transient events occur within the smoke window.
- Smoke clips cannot be submitted as valid acceptance evaluation results.

---

## 7. Font Rendering & Pillow Fallback

Text overlays (e.g. in `lines_and_text` tickers and telemetry) require crisp rendering without silent text loss:
1. **Drawtext Filter**: If the FFmpeg binary has `--enable-libfreetype` and contains the `drawtext`
   filter, `drawtext=fontfile=...` is used directly in the filter graph.
2. **Pillow Overlay Fallback**: If `drawtext` is missing (or when `--pillow-text` is specified),
   the generator rasterizes the text banner into an RGBA PNG with alpha transparency using Pillow
   in the active Python environment, then blends it onto the base video via FFmpeg's `overlay` filter.
3. **Font Identity**: The manifest records the resolved `font_file`, `font_name`, `font_sha256`
   (even when unrendered), and render method (`drawtext` vs `pillow_overlay`).

---

## 8. Command Reference & Examples

### Generate Recipe Manifest Only (Default)
```bash
python scripts/generate_smart_corpus.py \
  --ffmpeg /path/to/ffmpeg \
  --ffprobe /path/to/ffprobe \
  --font-file /path/to/font.ttf \
  --split all \
  --output-dir workdir/smart-corpus
```

### Full Production Render of Acceptance Split with HEVC Derivatives
```bash
python scripts/generate_smart_corpus.py \
  --ffmpeg /path/to/ffmpeg \
  --ffprobe /path/to/ffprobe \
  --font-file /path/to/font.ttf \
  --split acceptance \
  --render \
  --hevc-derivative \
  --output-dir workdir/smart-corpus-acceptance
```

### Fast Smoke Render with Explicit Binaries
```bash
python scripts/generate_smart_corpus.py \
  --ffmpeg /path/to/ffmpeg \
  --ffprobe /path/to/ffprobe \
  --font-file /path/to/font.ttf \
  --limit 3 \
  --width 320 \
  --height 240 \
  --duration 2.0 \
  --render \
  --output-dir workdir/smart-corpus-smoke
```
