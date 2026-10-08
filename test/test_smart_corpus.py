"""Tests for deterministic synthetic SMART video corpus generator."""

from __future__ import annotations

import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from typing import Any
from unittest.mock import patch

from PIL import ImageFont

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import scripts.generate_smart_corpus as generator  # noqa: E402
from core.smart.v1.evaluation import load_evaluation_manifest  # noqa: E402


def _create_dummy_fixtures(directory: Path) -> tuple[Path, Path, Path]:
    bin_dir = directory / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_ffmpeg = bin_dir / "ffmpeg"
    fake_ffprobe = bin_dir / "ffprobe"
    fake_font = directory / "fonts" / "test_font.ttf"
    fake_font.parent.mkdir(parents=True, exist_ok=True)

    fake_ffmpeg.write_bytes(b"fake executable")
    fake_ffprobe.write_bytes(b"fake executable")

    fake_font.write_bytes(b"dummy font data for testing")
    return fake_ffmpeg, fake_ffprobe, fake_font


class SmartCorpusGeneratorContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._drawtext = patch.object(generator, "detect_drawtext", return_value=True)
        self._drawtext.start()
        self.addCleanup(self._drawtext.stop)

    def test_no_imports_from_smart_algorithm(self) -> None:
        """The generator must be stdlib-only and must not import SMART algorithm modules."""
        gen_file = REPOSITORY_ROOT / "scripts" / "generate_smart_corpus.py"
        tree = ast.parse(gen_file.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertFalse(
                        alias.name.startswith("core.smart"),
                        f"Forbidden import found in AST: {alias.name}",
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self.assertFalse(
                        node.module.startswith("core.smart"),
                        f"Forbidden import from found in AST: {node.module}",
                    )

        probe_code = (
            "import sys; import scripts.generate_smart_corpus; "
            "loaded = [k for k in sys.modules if k.startswith('core.smart')]; "
            "assert not loaded, f'Loaded smart modules: {loaded}'"
        )
        res = subprocess.run(
            [sys.executable, "-c", probe_code],
            capture_output=True,
            text=True,
            cwd=str(REPOSITORY_ROOT),
        )
        self.assertEqual(res.returncode, 0, f"Generator imported SMART modules:\n{res.stderr}")

    def test_case_counts_and_splits(self) -> None:
        """Verify default 36 clips (12 per standard split) plus 6 long clips."""
        recipes = generator.ALL_RECIPES
        self.assertEqual(len(recipes), 42)

        dev_cases = [r for r in recipes if r.split == "development"]
        cal_cases = [r for r in recipes if r.split == "calibration"]
        acc_cases = [r for r in recipes if r.split == "acceptance"]
        long_cases = [r for r in recipes if r.split == "long"]

        self.assertEqual(len(dev_cases), 12)
        self.assertEqual(len(cal_cases), 12)
        self.assertEqual(len(acc_cases), 12)
        self.assertEqual(len(long_cases), 6)

        self.assertEqual(len(generator._filter_recipes("development", None)), 12)
        self.assertEqual(len(generator._filter_recipes("calibration", None)), 12)
        self.assertEqual(len(generator._filter_recipes("acceptance", None)), 12)
        self.assertEqual(len(generator._filter_recipes("long", None)), 6)
        self.assertEqual(len(generator._filter_recipes("all", None)), 42)
        self.assertEqual(len(generator._filter_recipes("all", 5)), 5)

    def test_group_isolation_and_disjointness(self) -> None:
        """Groups across splits must be strictly disjoint, holding out complex recipes for acceptance."""
        recipes = generator.ALL_RECIPES
        dev_groups = {r.group for r in recipes if r.split == "development"}
        cal_groups = {r.group for r in recipes if r.split == "calibration"}
        acc_groups = {r.group for r in recipes if r.split == "acceptance"}
        long_groups = {r.group for r in recipes if r.split == "long"}

        all_groups = [r.group for r in recipes]
        self.assertEqual(len(all_groups), 42)
        self.assertEqual(len(set(all_groups)), 42)

        self.assertEqual(dev_groups & cal_groups, set())
        self.assertEqual(dev_groups & acc_groups, set())
        self.assertEqual(cal_groups & acc_groups, set())
        self.assertEqual(long_groups & (dev_groups | cal_groups | acc_groups), set())

        acc_types = {r.recipe_type for r in recipes if r.split == "acceptance"}
        self.assertIn("heldout_tex_lissajous", acc_types)
        self.assertIn("heldout_tex_chaoticshear", acc_types)
        self.assertIn("heldout_lines_telemetry", acc_types)
        self.assertIn("heldout_lines_densegrid", acc_types)
        self.assertIn("heldout_dark_dithersteps", acc_types)
        self.assertIn("heldout_dark_shadowcreep", acc_types)
        self.assertIn("heldout_noise_burstysalt", acc_types)
        self.assertIn("heldout_noise_analogstreak", acc_types)
        self.assertIn("heldout_trans_strobe", acc_types)
        self.assertIn("heldout_trans_splitpush", acc_types)
        self.assertIn("heldout_event_microtransient", acc_types)
        self.assertIn("heldout_event_compoundburst", acc_types)

    def test_bounds_and_parameters(self) -> None:
        """Durations, FPS, bit depths, and dimensions must satisfy constraints."""
        for r in generator.ALL_RECIPES:
            if r.split in ("development", "calibration", "acceptance"):
                self.assertGreaterEqual(r.duration_sec, 45.0)
                self.assertLessEqual(r.duration_sec, 120.0)
            elif r.split == "long":
                self.assertEqual(r.duration_sec, 600.0)

            self.assertIn(r.fps, (24, 30, 60))
            self.assertIn(r.bit_depth, (8, 10))
            self.assertIn(r.pix_fmt, ("yuv420p", "yuv420p10le"))
            self.assertGreater(r.width, 0)
            self.assertGreater(r.height, 0)
            self.assertIn(r.scene_family, generator.SCENE_FAMILIES)

            for ev in r.event_times:
                self.assertGreaterEqual(ev["start_sec"], 0.0)
                self.assertLess(ev["start_sec"], ev["end_sec"])
                self.assertLessEqual(ev["end_sec"], r.duration_sec)

    def test_manifest_command_and_tool_identity(self) -> None:
        """Runner command must use sys.executable and run_smart_case.py; font SHA recorded unrendered."""
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            fake_ffmpeg, fake_ffprobe, fake_font = _create_dummy_fixtures(output_dir)
            m = generator.build_manifest_and_render(
                ffmpeg=fake_ffmpeg,
                ffprobe=fake_ffprobe,
                output_dir=output_dir,
                font_file=fake_font,
                split="development",
                limit=2,
                render=False,
                smoke_width=None,
                smoke_height=None,
                smoke_duration=None,
                generate_h264=False,
            )
            case = m["cases"][0]
            cmd = case["command"]
            self.assertEqual(cmd[0], sys.executable)
            self.assertEqual(Path(cmd[1]).parts[-2:], ("scripts", "run_smart_case.py"))
            self.assertIn("--source", cmd)
            self.assertIn("{source}", cmd)
            self.assertIn("--ffmpeg", cmd)
            self.assertIn("{ffmpeg}", cmd)
            self.assertIn("--ffprobe", cmd)
            self.assertIn("{ffprobe}", cmd)
            self.assertIn("--workdir", cmd)
            self.assertIn("{case_dir}", cmd)
            self.assertIn("--result", cmd)
            self.assertIn("{result_path}", cmd)
            self.assertNotIn("--case-id", cmd)

            # Font SHA must be recorded even when unrendered
            tool_id = case["metadata"]["tool_identity"]
            self.assertEqual(tool_id["font_file"], str(fake_font.resolve()))
            self.assertIsNotNone(tool_id["font_sha256"])
            self.assertEqual(len(tool_id["font_sha256"]), 64)

    def test_manifest_schema_and_loader_compatibility(self) -> None:
        """Manifest must adhere to schema_version 1 and load via load_evaluation_manifest."""
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            fake_ffmpeg, fake_ffprobe, fake_font = _create_dummy_fixtures(output_dir)
            m = generator.build_manifest_and_render(
                ffmpeg=fake_ffmpeg,
                ffprobe=fake_ffprobe,
                output_dir=output_dir,
                font_file=fake_font,
                split="development",
                limit=3,
                render=False,
                smoke_width=None,
                smoke_height=None,
                smoke_duration=None,
                generate_h264=False,
            )
            manifest_file = output_dir / "manifest.json"
            self.assertTrue(manifest_file.is_file())
            self.assertEqual(m["schema_version"], 1)

            # Assert compatibility with core manifest loader
            cases = load_evaluation_manifest(manifest_file)
            self.assertEqual(len(cases), 3)
            for c in cases:
                self.assertIsNone(c.measurement)
                self.assertTrue(len(c.command) > 0)

    def test_noise_and_gradients_bind_seed(self) -> None:
        """Filters must bind all_seed/seed to recipe.seed."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            fake_font = p / "font.ttf"
            fake_font.write_bytes(b"dummy font")
            # Check noise recipe binds all_seed
            r_noise = [r for r in generator.ALL_RECIPES if r.id == "smart-dev-07-noise-fine"][0]
            args, _ = generator.build_ffmpeg_filter_args(r_noise, fake_font, True, p)
            vf = args[args.index("-vf") + 1]
            self.assertIn(f"all_seed={r_noise.seed}", vf)

            # Check gradients recipe binds seed
            r_grad = [r for r in generator.ALL_RECIPES if r.id == "smart-cal-06-dark-conic"][0]
            args_g, _ = generator.build_ffmpeg_filter_args(r_grad, fake_font, True, p)
            lavfi_in = args_g[args_g.index("-i") + 1]
            self.assertIn(f"seed={r_grad.seed}", lavfi_in)

    def test_unknown_recipe_type_raises_value_error(self) -> None:
        """Unknown recipe type must raise ValueError, not silently substitute a fallback."""
        bogus_recipe = generator.CaseRecipe(
            id="bogus",
            split="development",
            group="bogus_group",
            scene_family="moving_textures",
            seed=9999,
            duration_sec=10.0,
            fps=30,
            width=320,
            height=240,
            bit_depth=8,
            event_times=(),
            recipe_type="non_existent_unhandled_type",
            recipe_params={},
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            fake_font = p / "font.ttf"
            fake_font.write_bytes(b"dummy font")
            with self.assertRaises(ValueError) as ctx:
                generator.build_ffmpeg_filter_args(bogus_recipe, fake_font, True, p)
            self.assertIn("Unknown or unhandled recipe type", str(ctx.exception))

    def test_smoke_overrides_flagged_not_acceptance(self) -> None:
        """Smoke overrides must flag cases as smoke and not valid for acceptance."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            fake_ffmpeg, fake_ffprobe, fake_font = _create_dummy_fixtures(output_dir)
            manifest_dict = generator.build_manifest_and_render(
                ffmpeg=fake_ffmpeg,
                ffprobe=fake_ffprobe,
                output_dir=output_dir,
                font_file=fake_font,
                split="acceptance",
                limit=3,
                render=False,
                smoke_width=320,
                smoke_height=240,
                smoke_duration=1.5,
                generate_h264=False,
            )
            for c in manifest_dict["cases"]:
                meta = c["metadata"]
                self.assertTrue(meta["smoke"])
                self.assertFalse(meta["acceptance_valid"])
                self.assertEqual(meta["width"], 320)
                self.assertEqual(meta["height"], 240)
                self.assertEqual(meta["duration_sec"], 1.5)

    def test_failure_behavior_invalid_binary_and_failed_ffmpeg(self) -> None:
        """Invalid ffmpeg paths and failed ffmpeg runs must raise clearly, not silently skip."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            bogus_tool = output_dir / "non_existent_tool"
            fake_ffmpeg, fake_ffprobe, fake_font = _create_dummy_fixtures(output_dir)

            # Exit code 1 when binary path does not exist
            exit_code = generator.main(
                [
                    "--ffmpeg",
                    str(bogus_tool),
                    "--ffprobe",
                    str(fake_ffprobe),
                    "--font-file",
                    str(fake_font),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            self.assertEqual(exit_code, 1)

            # Exit code 1 when font file does not exist
            exit_code_font = generator.main(
                [
                    "--ffmpeg",
                    str(fake_ffmpeg),
                    "--ffprobe",
                    str(fake_ffprobe),
                    "--font-file",
                    str(output_dir / "non_existent_font.ttf"),
                    "--output-dir",
                    str(output_dir),
                ]
            )
            self.assertEqual(exit_code_font, 1)

            # run_ffmpeg raises RuntimeError on failure without needing real ffmpeg
            with self.assertRaises(RuntimeError) as ctx:
                generator.run_ffmpeg(
                    [
                        sys.executable,
                        "-c",
                        "import sys; sys.stderr.write('simulated failure output'); sys.exit(2)",
                    ],
                    label="intentional failure",
                )
            self.assertIn("FFmpeg failed for intentional failure", str(ctx.exception))
            self.assertIn("simulated failure output", str(ctx.exception))

    def test_h264_derivative_failure_behavior(self) -> None:
        """Requesting h264 derivative with binary lacking libx264 must raise clearly."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            fake_ffmpeg, fake_ffprobe, _ = _create_dummy_fixtures(output_dir)
            src_mkv = output_dir / "src.mkv"
            src_mkv.write_bytes(b"dummy mkv")
            out_mp4 = output_dir / "out.mp4"

            with patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=[], returncode=1, stdout="", stderr="Unknown encoder 'libx264'"
                ),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    generator.render_h264_derivative(
                        ffmpeg=fake_ffmpeg,
                        ffprobe=fake_ffprobe,
                        source_mkv=src_mkv,
                        output_mp4=out_mp4,
                        fps=30,
                    )
                self.assertIn("Unknown encoder 'libx264'", str(ctx.exception))

    def test_explicit_arguments_required_on_cli(self) -> None:
        """CLI parser must require explicit --ffmpeg, --ffprobe, and --font-file without defaults."""
        parser = generator._parser()
        actions = {a.dest: a for a in parser._actions}
        self.assertTrue(actions["ffmpeg"].required)
        self.assertTrue(actions["ffprobe"].required)
        self.assertTrue(actions["font_file"].required)
        self.assertIsNone(actions["ffmpeg"].default)
        self.assertIsNone(actions["ffprobe"].default)
        self.assertIsNone(actions["font_file"].default)

    def test_font_resolution_no_os_defaults(self) -> None:
        """Font resolution must reject None and require an explicit valid font path."""
        with self.assertRaises(ValueError):
            generator.resolve_font_file(None)
        with self.assertRaises(FileNotFoundError):
            generator.resolve_font_file(Path("/non/existent/font.ttf"))


class SmartCorpusRenderSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._drawtext = patch.object(generator, "detect_drawtext", return_value=True)
        self._drawtext.start()
        self.addCleanup(self._drawtext.stop)

    def test_actual_render_smoke_pipeline(self) -> None:
        """Verify render pipeline, ffprobe metadata recording, and SHA256 computation with explicit binaries."""
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            fake_ffmpeg, fake_ffprobe, fake_font = _create_dummy_fixtures(output_dir)

            def mock_run_ffmpeg(cmd: list[str], label: str) -> None:
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"dummy_ffv1_rendered_clip_content")

            def mock_run_ffprobe(ffprobe: Path, video_path: Path) -> dict[str, Any]:
                name = video_path.name
                pix_fmt = "yuv420p"
                for r in generator.ALL_RECIPES:
                    if r.id in name:
                        pix_fmt = r.pix_fmt
                        break
                return {
                    "streams": [
                        {
                            "codec_name": "ffv1",
                            "width": 160,
                            "height": 120,
                            "pix_fmt": pix_fmt,
                        }
                    ],
                    "format": {
                        "filename": str(video_path),
                        "duration": "0.4",
                    },
                }

            with patch("scripts.generate_smart_corpus.run_ffmpeg", side_effect=mock_run_ffmpeg), \
                 patch("scripts.generate_smart_corpus.run_ffprobe", side_effect=mock_run_ffprobe):
                manifest_dict = generator.build_manifest_and_render(
                    ffmpeg=fake_ffmpeg,
                    ffprobe=fake_ffprobe,
                    output_dir=output_dir,
                    font_file=fake_font,
                    split="development",
                    limit=2,
                    render=True,
                    smoke_width=160,
                    smoke_height=120,
                    smoke_duration=0.4,
                    generate_h264=False,
                )
            cases = manifest_dict["cases"]
            self.assertEqual(len(cases), 2)

            for case_dict in cases:
                meta = case_dict["metadata"]
                render_info = meta["render"]
                self.assertTrue(render_info["rendered"])
                self.assertIsNotNone(render_info["sha256"])
                self.assertIsNotNone(render_info["render_argv"])
                self.assertGreater(render_info["size_bytes"], 0)

                video_path = output_dir / case_dict["source"]
                self.assertTrue(video_path.is_file())
                actual_sha = generator.compute_sha256(video_path)
                self.assertEqual(render_info["sha256"], actual_sha)

                streams = render_info["ffprobe"]["streams"]
                self.assertEqual(streams[0]["codec_name"], "ffv1")
                self.assertEqual(streams[0]["width"], 160)
                self.assertEqual(streams[0]["height"], 120)
                self.assertEqual(streams[0]["pix_fmt"], meta["pix_fmt"])

    def test_hevc_derivative_rendering(self) -> None:
        """Verify optional HEVC long-GOP derivative pipeline."""
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            fake_ffmpeg, fake_ffprobe, fake_font = _create_dummy_fixtures(output_dir)

            def mock_run_ffmpeg(cmd: list[str], label: str) -> None:
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"dummy_hevc_clip_content")

            def mock_run_ffprobe(ffprobe: Path, video_path: Path) -> dict[str, Any]:
                codec = "hevc" if "hevc" in str(video_path) else "ffv1"
                return {
                    "streams": [
                        {
                            "codec_name": codec,
                            "width": 160,
                            "height": 120,
                            "pix_fmt": "yuv420p",
                        }
                    ],
                    "format": {
                        "filename": str(video_path),
                        "duration": "0.3",
                    },
                }

            with patch("scripts.generate_smart_corpus.run_ffmpeg", side_effect=mock_run_ffmpeg), \
                 patch("scripts.generate_smart_corpus.run_ffprobe", side_effect=mock_run_ffprobe):
                manifest_dict = generator.build_manifest_and_render(
                    ffmpeg=fake_ffmpeg,
                    ffprobe=fake_ffprobe,
                    output_dir=output_dir,
                    font_file=fake_font,
                    split="development",
                    limit=1,
                    render=True,
                    smoke_width=160,
                    smoke_height=120,
                    smoke_duration=0.3,
                    generate_h264=False,
                    generate_hevc=True,
                )
            case_dict = manifest_dict["cases"][0]
            render_info = case_dict["metadata"]["render"]
            self.assertIn("hevc_derivative", render_info)
            hevc_meta = render_info["hevc_derivative"]
            self.assertEqual(hevc_meta["codec"], "hevc")
            self.assertIsNotNone(hevc_meta["sha256"])

            hevc_file = Path(hevc_meta["output_file"])
            self.assertTrue(hevc_file.is_file())
            self.assertEqual(generator.compute_sha256(hevc_file), hevc_meta["sha256"])
            streams = hevc_meta["ffprobe"]["streams"]
            self.assertEqual(streams[0]["codec_name"], "hevc")

    def test_motion_across_frames_verified(self) -> None:
        """Verify dynamic overlay motion evaluates per frame with explicit expressions."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            fake_font = p / "test_font.ttf"
            fake_font.write_bytes(b"dummy font")
            recipe = [r for r in generator.ALL_RECIPES if r.id == "smart-acc-01-tex-lissajous"][0]
            filter_args, _ = generator.build_ffmpeg_filter_args(
                recipe, fake_font, True, p, smoke_duration=0.5, smoke_width=160, smoke_height=120
            )
            self.assertIn("-filter_complex", filter_args)
            fc = filter_args[filter_args.index("-filter_complex") + 1]
            self.assertIn("eval=frame", fc)
            self.assertIn("sin(2*PI*t", fc)
            self.assertIn("sin(3*PI*t", fc)

    def test_pillow_text_overlay_fallback_render(self) -> None:
        """Verify Pillow rasterization overlay path renders text when forced."""
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            fake_ffmpeg, fake_ffprobe, fake_font = _create_dummy_fixtures(output_dir)

            def mock_run_ffmpeg(cmd: list[str], label: str) -> None:
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"dummy_pillow_overlay_video")

            def mock_run_ffprobe(ffprobe: Path, video_path: Path) -> dict[str, Any]:
                return {
                    "streams": [
                        {
                            "codec_name": "ffv1",
                            "width": 160,
                            "height": 120,
                            "pix_fmt": "yuv420p",
                        }
                    ],
                    "format": {
                        "filename": str(video_path),
                        "duration": "0.3",
                    },
                }

            with patch("scripts.generate_smart_corpus.run_ffmpeg", side_effect=mock_run_ffmpeg), \
                 patch("scripts.generate_smart_corpus.run_ffprobe", side_effect=mock_run_ffprobe), \
                 patch("PIL.ImageFont.truetype", return_value=ImageFont.load_default()):
                manifest_dict = generator.build_manifest_and_render(
                    ffmpeg=fake_ffmpeg,
                    ffprobe=fake_ffprobe,
                    output_dir=output_dir,
                    font_file=fake_font,
                    split="development",
                    limit=1,
                    render=True,
                    smoke_width=160,
                    smoke_height=120,
                    smoke_duration=0.3,
                    generate_h264=False,
                    force_pillow=True,
                )
            case_dict = manifest_dict["cases"][0]
            render_info = case_dict["metadata"]["render"]
            self.assertTrue(render_info["rendered"])
            font_render = render_info["font_render"]
            self.assertEqual(font_render["method"], "pillow_overlay")


if __name__ == "__main__":
    unittest.main()
