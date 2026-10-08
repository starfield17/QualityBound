from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.encoding import build_encode_plan
from core.models import (
    BackendChoice,
    EncodeOptions,
    MediaInfo,
    VideoFileItem,
)


def _media(path: Path) -> MediaInfo:
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


class ExplicitPlanningPathsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _source(self, directory: str, name: str) -> Path:
        source = self.root / directory / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"video")
        return source

    def _build(
        self,
        files: list[VideoFileItem],
        output_dir: Path | None,
    ):
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
                side_effect=lambda _ffprobe, source, **_kwargs: _media(source),
            ),
        ):
            plan = build_encode_plan(
                input_path=None,
                options=EncodeOptions(
                    backend=BackendChoice.CPU,
                    encoder_preset="slow",
                    overwrite=True,
                    copy_external_subtitles=False,
                ),
                output_dir=output_dir,
                workdir=self.root / "work",
                files=files,
            )
        self.assertTrue(
            all(item.skip_reason is None for item in plan.items),
            [item.skip_reason for item in plan.items],
        )
        return plan

    def test_without_output_dir_each_file_stays_beside_its_source(self) -> None:
        first = self._source("first", "clip.mov")
        second = self._source("second", "other.mov")

        plan = self._build(
            [
                VideoFileItem(first, Path("nested/clip.mov")),
                VideoFileItem(second, Path("other.mov")),
            ],
            None,
        )

        self.assertEqual(plan.items[0].output_path.parent, first.parent.resolve())
        self.assertEqual(plan.items[1].output_path.parent, second.parent.resolve())

    def test_same_stem_collisions_receive_stable_source_hashes(self) -> None:
        first = self._source("first", "clip.mov")
        second = self._source("second", "clip.mov")
        output = self.root / "output"
        files = [
            VideoFileItem(first, Path("clip.mov")),
            VideoFileItem(second, Path("clip.mov")),
        ]

        first_plan = self._build(files, output)
        second_plan = self._build(files, output)
        first_paths = [item.output_path for item in first_plan.items]

        self.assertEqual(len(set(first_paths)), 2)
        self.assertEqual(first_paths, [item.output_path for item in second_plan.items])
        self.assertTrue(all(path.parent == output.resolve() for path in first_paths))
        self.assertTrue(all(path.stem.startswith("clip_hevc-") for path in first_paths))

    def test_relative_directories_from_folder_drop_are_preserved(self) -> None:
        first = self._source("tree-a", "clip.mov")
        second = self._source("tree-b", "other.mov")
        output = self.root / "output"

        plan = self._build(
            [
                VideoFileItem(first, Path("nested-a/clip.mov")),
                VideoFileItem(second, Path("nested-b/deeper/other.mov")),
            ],
            output,
        )

        self.assertEqual(plan.items[0].output_path.parent, (output / "nested-a").resolve())
        self.assertEqual(
            plan.items[1].output_path.parent,
            (output / "nested-b" / "deeper").resolve(),
        )

    def test_duplicate_explicit_source_is_planned_once(self) -> None:
        source = self._source("same", "clip.mov")

        plan = self._build(
            [
                VideoFileItem(source, Path("clip.mov")),
                VideoFileItem(source, Path("duplicate/clip.mov")),
            ],
            self.root / "output",
        )

        self.assertEqual(len(plan.items), 1)

    def test_relative_path_cannot_escape_output_directory(self) -> None:
        source = self._source("source", "clip.mov")

        with self.assertRaisesRegex(ValueError, "must stay below"):
            self._build(
                [VideoFileItem(source, Path("../escape/clip.mov"))],
                self.root / "output",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
