# FFmpeg for QualityBound v2.6.0

Smart mode requires the project maintainer's dedicated FFmpeg distribution:
[starfield17/ffmpeg-vmaf-v1-builds](https://github.com/starfield17/ffmpeg-vmaf-v1-builds/releases/tag/ffmpeg-9.0.2-vmaf-v1.0.16-r1).
Use this distribution instead of replacing it with an arbitrary system or
third-party FFmpeg build. A build without the required filters and VMAF models
cannot run Smart analysis; having an executable named `ffmpeg` or merely enabling
`libvmaf` is not sufficient.

## Pinned Versions

The authoritative versions, source commits, download URLs, SHA-256 checksums and
license information are in [packaging/ffmpeg/manifest.json](../packaging/ffmpeg/manifest.json).

| Component | Current version |
| --- | --- |
| Distribution release | `ffmpeg-9.0.2-vmaf-v1.0.16-r1` |
| FFmpeg | `9.0.2` |
| libvmaf library | `3.2.0` |
| AV1 software decoder | dav1d `1.5.4` |
| Smart quality models | Netflix VMAF `v1.0.16` |
| Manifest / verification contract | `2` / `4` |

The libvmaf library version and VMAF model version are different identifiers.
Smart uses the pinned v1.0.16 models, including its normal/HFR 1080p and 4K
variants, plus the `libvmaf`, `siti` and `scdet` filters required by analysis.

## Build Provenance

- **macOS ARM64:** built by the maintainer in `starfield17/ffmpeg-vmaf-v1-builds`;
  source version `ffmpeg-n9.0.2+libvmaf-v3.2.0+dav1d-v1.5.4`.
- **Windows x86_64 / ARM64 and Linux x86_64 / ARM64:** maintainer-hosted trusted
  mirrors of BtbN builds; source version `n9.0.2-22-g46d8f462ee`.
- All five targets use the distribution recipe pinned to
  `f00ad587fffcf8e2ece8404bf1d146ff288a8cf2`. Exact platform-specific FFmpeg and
  libvmaf and dav1d commits and upstream build provenance are recorded in the manifest.
  Mirror builds include 22 commits after 9.0.2; macOS uses official 9.0.2 source.

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

A local version selector may also use `FFmpeg/current/bin/ffmpeg` and
`FFmpeg/current/bin/ffprobe`, with `current` pointing to one verified bundle.

Explicit GUI/CLI binary paths take priority over this directory; this directory
takes priority over system-installed tools. Ensure explicit paths also point to
the dedicated distribution. Packaged application releases use the pinned bundle
prepared and validated by `scripts/prepare_ffmpeg.py`.

Verification contract v4 requires HEVC and AV1 files to encode, decode all 24
frames without hardware acceleration, retain the declared geometry/pixel format,
and produce a finite CPU VMAF score for every frame. The macOS bundle statically
includes dav1d so AV1 scoring does not depend on hardware decoder availability.
