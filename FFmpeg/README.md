# FFmpeg for QualityBound v2.6.0

Smart mode requires the project maintainer's dedicated FFmpeg distribution:
[starfield17/ffmpeg-vmaf-v1-builds](https://github.com/starfield17/ffmpeg-vmaf-v1-builds/releases/tag/ffmpeg-9.0.1-vmaf-v1.0.16-r3).
Use this distribution instead of replacing it with an arbitrary system or
third-party FFmpeg build. A build without the required filters and VMAF models
cannot run Smart analysis; having an executable named `ffmpeg` or merely enabling
`libvmaf` is not sufficient.

## Pinned Versions

The authoritative versions, source commits, download URLs, SHA-256 checksums and
license information are in [packaging/ffmpeg/manifest.json](../packaging/ffmpeg/manifest.json).

| Component | Current version |
| --- | --- |
| Distribution release | `ffmpeg-9.0.1-vmaf-v1.0.16-r3` |
| FFmpeg | `9.0.1` |
| libvmaf library | `3.2.0` |
| Smart quality models | Netflix VMAF `v1.0.16` |
| Manifest / verification contract | `2` / `3` |

The libvmaf library version and VMAF model version are different identifiers.
Smart uses the pinned v1.0.16 models, including its normal/HFR 1080p and 4K
variants, plus the `libvmaf`, `siti` and `scdet` filters required by analysis.

## Build Provenance

- **macOS ARM64:** built by the maintainer in `starfield17/ffmpeg-vmaf-v1-builds`;
  source version `ffmpeg-n9.0.1+libvmaf-v3.2.0`.
- **Windows x86_64 / ARM64 and Linux x86_64 / ARM64:** maintainer-hosted trusted
  mirrors of BtbN builds; source version `n9.0.1-6-g9d4ca21220`.
- All five targets use the distribution recipe pinned to
  `18884f433a57d81130cb92c7817297df9f98ceed`. Exact platform-specific FFmpeg and
  libvmaf commits and upstream build provenance are recorded in the manifest.

## Local Installation

Download the archive matching your operating system and architecture from the
release above, verify its checksum against the manifest, and keep `ffmpeg` and
`ffprobe` from the same bundle together with any supplied runtime libraries.
Supported layouts relative to this directory are:

```text
FFmpeg/ffmpeg(.exe)
FFmpeg/ffprobe(.exe)
```

or:

```text
FFmpeg/bin/ffmpeg(.exe)
FFmpeg/bin/ffprobe(.exe)
```

Explicit GUI/CLI binary paths take priority over this directory; this directory
takes priority over system-installed tools. Ensure explicit paths also point to
the dedicated distribution. Packaged application releases use the pinned bundle
prepared and validated by `scripts/prepare_ffmpeg.py`.
