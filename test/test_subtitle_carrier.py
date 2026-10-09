"""Container/subtitle compatibility: refuse early instead of after the encode.

N9 ← S3: a source whose subtitle codec the target container cannot carry is
refused in planning, with the reason, before any encode runs.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.encoding import build_encode_plan
from core.ffmpeg.probe import probe_media_info
from core.media.subtitles import mp4_incapable_subtitle_codecs
from core.media.validation import validate_subtitle_carrier
from core.models import (
    BackendChoice,
    ContainerChoice,
    EncodeOptions,
    MediaInfo,
)


def _media(path: Path, subtitle_codecs: tuple[str, ...] = ()) -> MediaInfo:
    return MediaInfo(
        path=path,
        duration=10.0,
        format_bitrate_bps=2_000_000,
        video_bitrate_bps=1_800_000,
        audio_bitrate_bps=128_000,
        width=1280,
        height=720,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        subtitle_codecs=subtitle_codecs,
    )


def _options(
    *,
    container: ContainerChoice = ContainerChoice.MP4,
    copy_subtitles: bool = True,
) -> EncodeOptions:
    return EncodeOptions(
        backend=BackendChoice.CPU,
        encoder_preset="slow",
        overwrite=True,
        copy_external_subtitles=False,
        container=container,
        copy_subtitles=copy_subtitles,
    )


def _capabilities() -> dict:
    return {
        "hwaccels": [],
        "codecs": {
            "hevc": [
                {
                    "backend": BackendChoice.CPU.value,
                    "encoder": "libx265",
                    "preset_choices": ["slow"],
                }
            ],
            "av1": [],
        },
    }


class SubtitleCodecClassificationTestCase(unittest.TestCase):
    def test_bitmap_codecs_are_named_once_and_deduplicated(self) -> None:
        self.assertEqual(
            mp4_incapable_subtitle_codecs(
                ("hdmv_pgs_subtitle", "subrip", "hdmv_pgs_subtitle", "dvd_subtitle")
            ),
            ("hdmv_pgs_subtitle", "dvd_subtitle"),
        )

    def test_text_and_unknown_codecs_are_not_claimed(self) -> None:
        self.assertEqual(mp4_incapable_subtitle_codecs(("subrip", "ass", "mov_text")), ())


class SubtitleCarrierValidationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.media = _media(Path("source.mkv"), ("hdmv_pgs_subtitle",))

    def test_mp4_with_bitmap_subtitles_is_refused_with_the_reason(self) -> None:
        with self.assertRaises(RuntimeError) as raised:
            validate_subtitle_carrier(self.media, _options(container=ContainerChoice.MP4))
        message = str(raised.exception)
        self.assertIn("hdmv_pgs_subtitle", message)
        self.assertIn("mov_text", message)
        self.assertIn("MKV", message)

    def test_mkv_carries_bitmap_subtitles(self) -> None:
        validate_subtitle_carrier(self.media, _options(container=ContainerChoice.MKV))

    def test_disabled_subtitle_copying_needs_no_container_change(self) -> None:
        validate_subtitle_carrier(
            self.media,
            _options(container=ContainerChoice.MP4, copy_subtitles=False),
        )

    def test_text_subtitles_into_mp4_are_allowed(self) -> None:
        validate_subtitle_carrier(
            _media(Path("source.mkv"), ("subrip",)),
            _options(container=ContainerChoice.MP4),
        )


class ProbeSubtitleCodecsTestCase(unittest.TestCase):
    def _probe(self, streams: list[dict[str, object]]) -> MediaInfo:
        payload = {
            "format": {"duration": "10.0", "bit_rate": "2000000"},
            "streams": streams,
        }
        with patch(
            "core.ffmpeg.probe._run_command",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout=json.dumps(payload), stderr=""
            ),
        ):
            return probe_media_info(Path("ffprobe"), Path("source.mkv"))

    def test_subtitle_codecs_are_collected_in_stream_order(self) -> None:
        media = self._probe(
            [
                {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720},
                {"codec_type": "subtitle", "codec_name": "subrip"},
                {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            ]
        )
        self.assertEqual(media.subtitle_codecs, ("subrip", "hdmv_pgs_subtitle"))

    def test_sources_without_subtitles_report_none(self) -> None:
        media = self._probe(
            [{"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720}]
        )
        self.assertEqual(media.subtitle_codecs, ())


class PlanningRefusalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "clip.mkv"
        self.source.write_bytes(b"video")

    def _plan(self, options: EncodeOptions, subtitle_codecs: tuple[str, ...]):
        with (
            patch(
                "core.encoding.planning.discover_ffmpeg_tools",
                return_value=(self.root / "ffmpeg", self.root / "ffprobe"),
            ),
            patch(
                "core.encoding.planning.ensure_encoder_capabilities",
                return_value=_capabilities(),
            ),
            patch(
                "core.encoding.planning.probe_media_info",
                return_value=_media(self.source, subtitle_codecs),
            ),
        ):
            return build_encode_plan(
                input_path=self.source,
                options=options,
                output_dir=self.root / "out",
                workdir=self.root / "work",
            )

    def test_bitmap_subtitles_into_mp4_become_a_skipped_item_before_encoding(self) -> None:
        plan = self._plan(_options(container=ContainerChoice.MP4), ("hdmv_pgs_subtitle",))
        item = plan.items[0]
        self.assertIsNotNone(item.skip_reason)
        assert item.skip_reason is not None
        self.assertIn("hdmv_pgs_subtitle", item.skip_reason)
        self.assertIn("MKV", item.skip_reason)

    def test_the_same_source_plans_for_mkv(self) -> None:
        plan = self._plan(_options(container=ContainerChoice.MKV), ("hdmv_pgs_subtitle",))
        self.assertIsNone(plan.items[0].skip_reason)

    def test_text_subtitles_into_mp4_still_plan(self) -> None:
        plan = self._plan(_options(container=ContainerChoice.MP4), ("subrip",))
        self.assertIsNone(plan.items[0].skip_reason)


if __name__ == "__main__":
    unittest.main()
