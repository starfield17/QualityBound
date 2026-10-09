from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from core.smart.v1.runtime import AnalysisCapabilities, format_analysis_capability_report
from core.smart.v1.vmaf import VMAF_PRODUCTION_MODELS
from scripts.prepare_ffmpeg import (
    binary_architecture,
    load_manifest,
    prepare_target,
    verify_anamorphic_normalization,
    verify_capabilities,
    verify_codec_roundtrips,
)


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TARGETS = {
    "windows-x86_64",
    "windows-arm64",
    "linux-x86_64",
    "linux-arm64",
    "macos-arm64",
}
OWNED_RELEASE_URL_PREFIX = (
    "https://github.com/starfield17/ffmpeg-vmaf-v1-builds/releases/download/"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pe_binary(machine: int) -> bytes:
    payload = bytearray(512)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", payload, 0x84, machine)
    return bytes(payload)


class FFmpegManifestTestCase(unittest.TestCase):
    def test_script_entrypoint_imports_from_an_isolated_interpreter(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / "prepare_ffmpeg.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_manifest_pins_all_release_targets(self) -> None:
        manifest = load_manifest(ROOT / "packaging" / "ffmpeg" / "manifest.json")
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["verification_contract_version"], 4)
        self.assertEqual(manifest["ffmpeg_version"], "9.0.2")
        targets = manifest["targets"]
        assert isinstance(targets, dict)
        self.assertEqual(set(targets), EXPECTED_TARGETS)
        self.assertNotIn("macos-x86_64", targets)
        for target in targets.values():
            self.assertIn(target["source_kind"], {"build", "mirror"})
            self.assertRegex(target["ffmpeg_commit"], r"^[0-9a-f]{40}$")
            self.assertRegex(target["libvmaf_commit"], r"^[0-9a-f]{40}$")
            self.assertIn(target["ffmpeg_commit"], target["ffmpeg_source"])
            self.assertIn(target["libvmaf_commit"], target["libvmaf_source"])
            self.assertTrue(target["archives"])
            for archive in target["archives"]:
                self.assertRegex(archive["sha256"], r"^[0-9a-f]{64}$")
                self.assertNotEqual(archive["sha256"], "0" * 64)
                self.assertNotIn("/latest/", archive["url"])
                self.assertTrue(
                    archive["url"].startswith(OWNED_RELEASE_URL_PREFIX),
                    archive["url"],
                )


class CodecRoundtripTestCase(unittest.TestCase):
    def test_roundtrip_rejects_missing_decoder_and_incomplete_scores(self):
        for failure in ("decoder", "frames", "score", "frame_order"):
            with self.subTest(failure=failure):
                def run(command, failure=failure, **kwargs):
                    if "-show_streams" in command:
                        codec = Path(command[-1]).stem
                        return json.dumps({"streams": [{"codec_name": codec, "width": 320,
                                                       "height": 180, "pix_fmt": "yuv420p"}]})
                    if "framemd5" in command:
                        if failure == "decoder":
                            raise RuntimeError("decoder unavailable")
                        return "\n".join("0, 0, 0, 1, 86400, hash" for _ in range(24))
                    if "-filter_complex" in command:
                        graph = command[command.index("-filter_complex") + 1]
                        scores = [{"frameNum": n, "metrics": {"vmaf": 95.0}}
                                  for n in range(23 if failure == "frames" else 24)]
                        if failure == "score":
                            scores[0]["metrics"]["vmaf"] = float("nan")
                        if failure == "frame_order":
                            scores[0]["frameNum"] = 1
                        (kwargs["cwd"] / graph.split("log_path=")[-1]).write_text(json.dumps({"frames": scores}))
                    return ""
                with patch("scripts.prepare_ffmpeg._run_checked", side_effect=run), self.assertRaises(RuntimeError):
                    verify_codec_roundtrips(Path("ffmpeg"), Path("ffprobe"))




class FFmpegPreparationTestCase(unittest.TestCase):
    def _fixture_manifest(self, root: Path, *, archive_digest: str | None = None) -> dict[str, object]:
        archive = root / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("../../unsafe/ffmpeg.exe", _pe_binary(0x8664))
            package.writestr("nested/bin/ffprobe.exe", _pe_binary(0x8664))

        license_path = root / "LICENSE.md"
        license_path.write_text("license", encoding="utf-8")
        copying_path = root / "COPYING.GPLv3"
        copying_path.write_text("copying", encoding="utf-8")
        vmaf_license_path = root / "VMAF-LICENSE.txt"
        vmaf_license_path.write_text("vmaf license", encoding="utf-8")
        dav1d_license_path = root / "DAV1D-LICENSE.txt"
        dav1d_license_path.write_text("dav1d license", encoding="utf-8")
        ffmpeg_commit = "a" * 40
        libvmaf_commit = "b" * 40
        return {
            "schema_version": 2,
            "verification_contract_version": 4,
            "ffmpeg_version": "9.0.2",
            "licenses": [
                {"component": "dav1d", "name": dav1d_license_path.name,
                 "url": dav1d_license_path.as_uri(), "sha256": _sha256(dav1d_license_path)},
                {
                    "component": "FFmpeg",
                    "name": license_path.name,
                    "url": license_path.as_uri(),
                    "sha256": _sha256(license_path),
                },
                {
                    "component": "FFmpeg",
                    "name": copying_path.name,
                    "url": copying_path.as_uri(),
                    "sha256": _sha256(copying_path),
                },
                {
                    "component": "Netflix VMAF",
                    "name": vmaf_license_path.name,
                    "url": vmaf_license_path.as_uri(),
                    "sha256": _sha256(vmaf_license_path),
                },
            ],
            "targets": {
                "windows-x86_64": {
                    "platform": "windows",
                    "architecture": "x86_64",
                    "source_kind": "mirror",
                    "provider": "fixture",
                    "source_version": "fixture",
                    "build_recipe": "https://example.invalid/build",
                    "ffmpeg_commit": ffmpeg_commit,
                    "ffmpeg_source": f"https://example.invalid/ffmpeg/{ffmpeg_commit}",
                    "libvmaf_version": "3.2.0",
                    "libvmaf_commit": libvmaf_commit,
                    "libvmaf_source": f"https://example.invalid/vmaf/{libvmaf_commit}",
                    "upstream_build_recipe": "https://example.invalid/upstream-build",
                    "upstream_release": "https://example.invalid/upstream-release",
                    "archives": [
                        {
                            "url": archive.as_uri(),
                            "sha256": archive_digest or _sha256(archive),
                            "format": "zip",
                        }
                    ],
                }
            },
        }

    def test_prepares_binaries_licenses_and_source_metadata_inside_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "workdir").mkdir()
            manifest = self._fixture_manifest(root)
            output = root / "workdir" / "ffmpeg" / "windows-x86_64"

            prepare_target(
                "windows-x86_64",
                output,
                manifest=manifest,
                root=root,
                run_capability_checks=False,
            )

            ffmpeg = output / "bin" / "ffmpeg.exe"
            ffprobe = output / "bin" / "ffprobe.exe"
            self.assertEqual(binary_architecture(ffmpeg), "x86_64")
            self.assertEqual(binary_architecture(ffprobe), "x86_64")
            self.assertTrue((output / "LICENSES" / "LICENSE.md").is_file())
            self.assertTrue((output / "LICENSES" / "COPYING.GPLv3").is_file())
            self.assertTrue((output / "LICENSES" / "VMAF-LICENSE.txt").is_file())
            self.assertTrue((output / "LICENSES" / "DAV1D-LICENSE.txt").is_file())
            source = json.loads((output / "SOURCE.json").read_text(encoding="utf-8"))
            self.assertEqual(source["schema_version"], 2)
            self.assertEqual(source["target"], "windows-x86_64")
            self.assertFalse((root / "unsafe").exists())

    def test_rejects_output_outside_project_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "workdir").mkdir()
            with self.assertRaisesRegex(ValueError, "inside"):
                prepare_target(
                    "windows-x86_64",
                    root / "outside",
                    manifest=self._fixture_manifest(root),
                    root=root,
                    run_capability_checks=False,
                )

    def test_checksum_mismatch_fails_before_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "workdir").mkdir()
            manifest = self._fixture_manifest(root, archive_digest="0" * 63 + "1")
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                prepare_target(
                    "windows-x86_64",
                    root / "workdir" / "output",
                    manifest=manifest,
                    root=root,
                    run_capability_checks=False,
                )

    def test_placeholder_archive_digest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "workdir").mkdir()
            manifest = self._fixture_manifest(root, archive_digest="0" * 64)
            with self.assertRaisesRegex(ValueError, "Invalid archive"):
                prepare_target(
                    "windows-x86_64",
                    root / "workdir" / "output",
                    manifest=manifest,
                    root=root,
                    run_capability_checks=False,
                )

    def test_optional_analysis_capabilities_are_reported_not_required(self) -> None:
        report = format_analysis_capability_report(
            AnalysisCapabilities(
                libvmaf=True,
                libvmaf_cuda=False,
                loopback_decoder=False,
                hwaccels=frozenset(),
                filters=frozenset({"libvmaf"}),
                encoders=frozenset({"libx265"}),
                scale_vt=False,
                scale_cuda=False,
                videotoolbox_hwaccel=False,
                cuda_hwaccel=False,
                videotoolbox_prio_speed=False,
            )
        )
        self.assertIn("CPU VMAF", report)
        self.assertIn("CUDA VMAF filter (not enabled for v1)", report)
        self.assertIn("Loopback decoder", report)

    def test_capability_verification_smokes_all_v1_models_with_shared_builder(self) -> None:
        commands: list[list[str]] = []

        def fake_run(command: list[str]) -> str:
            commands.append(command)
            if "-version" in command:
                return "--enable-gpl --enable-version3"
            if "-filters" in command:
                return " T.. libvmaf V->V\n T.. siti V->V\n T.. scdet V->V\n"
            if "-encoders" in command:
                return "libx265 libsvtav1"
            if "-filter_complex" in command:
                graph = command[command.index("-filter_complex") + 1]
                if "siti" in graph:
                    return (
                        "frame:0 pts:0 pts_time:0\n"
                        "lavfi.siti.si=10\nlavfi.siti.ti=0\nlavfi.scd.score=0\n"
                        "frame:1 pts:1 pts_time:0.083\n"
                        "lavfi.siti.si=12\nlavfi.siti.ti=2\nlavfi.scd.score=0\n"
                        "frame:2 pts:2 pts_time:0.5\n"
                        "lavfi.siti.si=20\nlavfi.siti.ti=8\nlavfi.scd.score=20\n"
                        "lavfi.scd.time=0.5\n"
                    )
                return "VMAF score: 100.0"
            if "-vf" in command:
                return (
                    "Video: wrapped_avframe, yuv420p10le, "
                    "1920x1080 [SAR 1:1 DAR 16:9]\n"
                    "lavfi.signalstats.YMIN=940\n"
                )
            return ""

        capabilities = AnalysisCapabilities(
            libvmaf=True,
            libvmaf_cuda=False,
            loopback_decoder=False,
            hwaccels=frozenset(),
            filters=frozenset({"libvmaf", "siti", "scdet"}),
            encoders=frozenset({"libx265", "libsvtav1"}),
            scale_vt=False,
            scale_cuda=False,
            videotoolbox_hwaccel=False,
            cuda_hwaccel=False,
            videotoolbox_prio_speed=False,
        )
        with (
            patch("scripts.prepare_ffmpeg._run_checked", side_effect=fake_run),
            patch("scripts.prepare_ffmpeg.verify_codec_roundtrips") as roundtrips,
            patch("scripts.prepare_ffmpeg.detect_analysis_capabilities", return_value=capabilities),
        ):
            verify_capabilities(Path("ffmpeg"), Path("ffprobe"))

        roundtrips.assert_called_once_with(Path("ffmpeg"), Path("ffprobe"))
        probes = [
            command
            for command in commands
            if "-filter_complex" in command
            and "libvmaf" in command[command.index("-filter_complex") + 1]
        ]
        self.assertEqual(len(probes), len(VMAF_PRODUCTION_MODELS))
        for command, model in zip(probes, VMAF_PRODUCTION_MODELS):
            graph = command[command.index("-filter_complex") + 1]
            self.assertIn(model.name, graph)
            self.assertIn(f"scale={model.display_width}:{model.display_height}", graph)
            self.assertIn("format=yuv420p10le", graph)
            self.assertIn("cambi.enc_width=320", graph)
            self.assertIn("cambi.enc_height=180", graph)
            self.assertIn("cambi.enc_bitdepth=8", graph)
            expected_fps = 60 if model.hfr else 30
            expected_frames = "12" if model.hfr else "6"
            self.assertEqual(
                command.count(f"testsrc2=size=320x180:rate={expected_fps}:duration=1"),
                2,
            )
            self.assertEqual(command[command.index("-frames:v") + 1], expected_frames)

        anamorphic = [command for command in commands if "-vf" in command]
        self.assertEqual(len(anamorphic), 1)
        self.assertIn("setsar=64/45", anamorphic[0][anamorphic[0].index("-vf") + 1])

    def test_real_anamorphic_normalization_when_ffmpeg_is_provided(self) -> None:
        value = os.environ.get("VMAF_INTEGRATION_FFMPEG")
        if not value:
            self.skipTest("VMAF_INTEGRATION_FFMPEG is not configured")
        self.assertGreaterEqual(verify_anamorphic_normalization(Path(value)), 800)


if __name__ == "__main__":
    unittest.main(verbosity=2)
