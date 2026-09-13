from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_nuitka import (
    _build_environment,
    build_nuitka_command,
    build_paths,
    clean_generated_paths,
    final_package_dir,
    find_ffmpeg_pair,
    locate_app_bundle,
    locate_dmg,
    normalize_version,
    normalized_machine,
    patch_nuitka_windows_arm64_clang_probe,
    refresh_macos_dmg,
    resolve_macos_target_arch,
    resolve_windows_compiler,
    stage_release_resources,
)


class NuitkaBuildCommandTestCase(unittest.TestCase):
    def test_common_command_contains_required_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            command = build_nuitka_command(
                "1.2.3",
                root=root,
                platform_name="linux",
            )

            self.assertIn("--mode=standalone", command)
            self.assertIn("--enable-plugin=pyside6", command)
            self.assertIn("--include-package=cli", command)
            self.assertIn("--include-package=core", command)
            self.assertIn("--include-package=gui", command)
            self.assertIn("--output-filename=qualitybound", command)
            self.assertTrue(
                any(option.startswith("--report=") for option in command)
            )

    def test_build_caches_are_kept_under_project_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            environment = _build_environment(root)
        self.assertEqual(environment["NUITKA_CACHE_DIR"], str(root / "workdir" / "nuitka-cache"))
        self.assertEqual(environment["CCACHE_DIR"], str(root / "workdir" / "ccache"))

    def test_windows_command_contains_metadata_and_console_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            icon = root / "packaging" / "assets" / "app.ico"
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b"ico")
            command = build_nuitka_command(
                "1.2.3",
                root=root,
                platform_name="win32",
                machine="x86_64",
                windows_compiler="mingw64",
            )

            self.assertIn("--mingw64", command)
            self.assertNotIn("--msvc=latest", command)
            self.assertIn("--windows-console-mode=attach", command)
            self.assertIn("--product-name=QualityBound", command)
            self.assertIn("--file-description=QualityBound", command)
            self.assertIn("--company-name=starfield17", command)
            self.assertIn("--product-version=1.2.3.0", command)
            self.assertIn("--file-version=1.2.3.0", command)
            self.assertIn(f"--windows-icon-from-ico={icon}", command)

    def test_windows_command_can_select_msvc(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            command = build_nuitka_command(
                "1.2.3",
                root=Path(temp_dir).resolve(),
                platform_name="win32",
                windows_compiler="msvc",
            )

        self.assertIn("--msvc=latest", command)
        self.assertNotIn("--mingw64", command)

    def test_windows_command_can_select_clang_without_conflicting_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            command = build_nuitka_command(
                "1.2.3",
                root=Path(temp_dir).resolve(),
                platform_name="win32",
                windows_compiler="clang",
                machine="arm64",
            )

        self.assertIn("--clang", command)
        self.assertNotIn("--mingw64", command)
        self.assertNotIn("--msvc=latest", command)

    def test_windows_compiler_auto_resolves_by_native_machine(self) -> None:
        self.assertEqual(normalized_machine("AMD64"), "x86_64")
        self.assertEqual(normalized_machine("x86_64"), "x86_64")
        self.assertEqual(normalized_machine("ARM64"), "arm64")
        self.assertEqual(normalized_machine("aarch64"), "arm64")
        self.assertEqual(
            resolve_windows_compiler("auto", machine="AMD64", python_version=(3, 12)),
            "mingw64",
        )
        self.assertEqual(
            resolve_windows_compiler("auto", machine="x86_64", python_version=(3, 12)),
            "mingw64",
        )
        self.assertEqual(
            resolve_windows_compiler("auto", machine="x86_64", python_version=(3, 13)),
            "msvc",
        )
        self.assertEqual(resolve_windows_compiler("auto", machine="ARM64"), "clang")
        self.assertEqual(resolve_windows_compiler("auto", machine="aarch64"), "clang")
        self.assertEqual(resolve_windows_compiler("msvc", machine="arm64"), "msvc")
        self.assertEqual(resolve_windows_compiler("clang", machine="x86_64"), "clang")

    def test_windows_command_rejects_unknown_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "Unsupported Windows compiler"):
                build_nuitka_command(
                    "1.2.3",
                    root=Path(temp_dir).resolve(),
                    platform_name="win32",
                    windows_compiler="unknown",
                )

    def test_patches_pinned_nuitka_arm64_clang_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            nuitka_root = Path(temp_dir)
            probe_path = nuitka_root / "build" / "SconsUtils.py"
            probe_path.parent.mkdir()
            probe_path.write_text(
                'elif b"ARM64" in process_result.stderr:\n',
                encoding="utf-8",
            )

            self.assertTrue(
                patch_nuitka_windows_arm64_clang_probe(
                    platform_name="win32",
                    machine="aarch64",
                    nuitka_root=nuitka_root,
                )
            )
            self.assertIn(
                'b"aarch64" in process_result.stdout',
                probe_path.read_text(encoding="utf-8"),
            )
            self.assertFalse(
                patch_nuitka_windows_arm64_clang_probe(
                    platform_name="win32",
                    machine="arm64",
                    nuitka_root=nuitka_root,
                )
            )

    def test_does_not_patch_nuitka_probe_on_other_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            nuitka_root = Path(temp_dir)
            probe_path = nuitka_root / "build" / "SconsUtils.py"
            probe_path.parent.mkdir()
            original = 'elif b"ARM64" in process_result.stderr:\n'
            probe_path.write_text(original, encoding="utf-8")

            self.assertFalse(
                patch_nuitka_windows_arm64_clang_probe(
                    platform_name="darwin",
                    machine="arm64",
                    nuitka_root=nuitka_root,
                )
            )
            self.assertEqual(probe_path.read_text(encoding="utf-8"), original)

    def test_macos_app_command_and_paths_are_native(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            icon = root / "packaging" / "assets" / "app.icns"
            icon.parent.mkdir(parents=True)
            icon.write_bytes(b"png")
            command = build_nuitka_command(
                "1.2.3",
                root=root,
                platform_name="darwin",
                macos_app_bundle=True,
                target_arch="arm64",
                machine="arm64",
            )
            paths = build_paths(
                root=root,
                platform_name="darwin",
                macos_app_bundle=True,
                target_arch="arm64",
                machine="arm64",
            )

        self.assertIn("--mode=app-dist", command)
        self.assertNotIn("--mode=standalone", command)
        self.assertIn("--macos-app-name=QualityBound", command)
        self.assertIn("--macos-signed-app-name=com.starfield17.QualityBound", command)
        self.assertIn("--macos-app-mode=gui", command)
        self.assertIn("--macos-app-version=1.2.3", command)
        self.assertIn("--macos-target-arch=arm64", command)
        self.assertIn("--macos-app-create-dmg", command)
        self.assertIn(f"--macos-app-icon={icon}", command)
        self.assertEqual(paths.package_dir, root / "dist" / "QualityBound.app")
        self.assertEqual(
            paths.executable_path,
            root / "dist" / "QualityBound.app" / "Contents" / "MacOS" / "qualitybound",
        )
        self.assertEqual(paths.resources_dir, paths.package_dir / "Contents" / "Resources")
        self.assertEqual(paths.dmg_path, root / "dist" / "qualitybound.dmg")
        self.assertEqual(paths.target_arch, "arm64")

    def test_apple_silicon_command_disables_nuitka_downloaded_x86_ccache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            arm64_command = build_nuitka_command(
                "1.2.3",
                root=Path(temp_dir).resolve(),
                platform_name="darwin",
                machine="arm64",
            )
            intel_command = build_nuitka_command(
                "1.2.3",
                root=Path(temp_dir).resolve(),
                platform_name="darwin",
                machine="x86_64",
            )

        self.assertIn("--disable-cache=ccache", arm64_command)
        self.assertNotIn("--disable-cache=ccache", intel_command)

    def test_macos_app_build_rejects_non_macos_platform_and_cross_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            with self.assertRaisesRegex(ValueError, "only supported on macOS"):
                build_nuitka_command(
                    "1.2.3",
                    root=root,
                    platform_name="linux",
                    macos_app_bundle=True,
                    target_arch="arm64",
                    machine="arm64",
                )
            with self.assertRaisesRegex(ValueError, "native runner architecture"):
                resolve_macos_target_arch("x86_64", machine="arm64")


class NuitkaVersionTestCase(unittest.TestCase):
    def test_three_part_version_gets_windows_build_component(self) -> None:
        self.assertEqual(normalize_version("1.2.3"), "1.2.3.0")

    def test_four_part_version_is_unchanged(self) -> None:
        self.assertEqual(normalize_version("1.2.3.4"), "1.2.3.4")

    def test_nonnumeric_version_fails_clearly(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid version"):
            normalize_version("1.2.3-beta")


class NuitkaStagingTestCase(unittest.TestCase):
    def _make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        (root / "config" / "i18n").mkdir(parents=True)
        (root / "config" / "i18n" / "en.json").write_text("{}", encoding="utf-8")
        (root / "README.md").write_text("README", encoding="utf-8")
        (root / "LICENSE").write_text("MIT License\n", encoding="utf-8")
        assets = root / "packaging" / "assets"
        assets.mkdir(parents=True)
        (assets / "app.svg").write_text("<svg/>", encoding="utf-8")
        (root / "workdir").mkdir()
        (root / "workdir" / "runtime-only.txt").write_text("not bundled", encoding="utf-8")
        package_dir = root / "dist" / "qualitybound"
        package_dir.mkdir(parents=True)
        return temp_dir, root, package_dir

    def test_staging_copies_config_and_readme_but_not_workdir(self) -> None:
        temp_dir, root, package_dir = self._make_root()
        with temp_dir:
            stage_release_resources(package_dir, root=root, platform_name="linux")

            self.assertEqual((package_dir / "config" / "i18n" / "en.json").read_text(encoding="utf-8"), "{}")
            self.assertEqual((package_dir / "README.md").read_text(encoding="utf-8"), "README")
            self.assertEqual((package_dir / "LICENSE").read_text(encoding="utf-8"), "MIT License\n")
            self.assertTrue((package_dir / "assets" / "app.svg").is_file())
            self.assertFalse((package_dir / "workdir").exists())

    def test_staging_copies_complete_matching_ffmpeg_pair(self) -> None:
        temp_dir, root, package_dir = self._make_root()
        with temp_dir:
            (root / "FFmpeg" / "bin").mkdir(parents=True)
            (root / "FFmpeg" / "bin" / "ffmpeg").write_text("ffmpeg", encoding="utf-8")
            (root / "FFmpeg" / "bin" / "ffprobe").write_text("ffprobe", encoding="utf-8")

            self.assertIsNotNone(find_ffmpeg_pair(root / "FFmpeg", "linux"))
            stage_release_resources(package_dir, root=root, platform_name="linux")

            self.assertTrue((package_dir / "FFmpeg" / "bin" / "ffmpeg").is_file())
            self.assertTrue((package_dir / "FFmpeg" / "bin" / "ffprobe").is_file())

    def test_staging_skips_incomplete_ffmpeg_pair(self) -> None:
        temp_dir, root, package_dir = self._make_root()
        with temp_dir:
            (root / "FFmpeg").mkdir()
            (root / "FFmpeg" / "ffmpeg").write_text("ffmpeg", encoding="utf-8")

            stage_release_resources(package_dir, root=root, platform_name="linux")

            self.assertFalse((package_dir / "FFmpeg").exists())

    def test_staging_can_require_explicit_ffmpeg_directory(self) -> None:
        temp_dir, root, package_dir = self._make_root()
        with temp_dir:
            explicit = root / "workdir" / "ffmpeg-release"
            explicit.mkdir()
            with self.assertRaisesRegex(RuntimeError, "No complete compatible FFmpeg pair"):
                stage_release_resources(
                    package_dir,
                    root=root,
                    platform_name="linux",
                    ffmpeg_dir=explicit,
                    require_ffmpeg=True,
                )

            (explicit / "bin").mkdir()
            (explicit / "bin" / "ffmpeg").write_text("ffmpeg", encoding="utf-8")
            (explicit / "bin" / "ffprobe").write_text("ffprobe", encoding="utf-8")
            stage_release_resources(
                package_dir,
                root=root,
                platform_name="linux",
                ffmpeg_dir=explicit,
                require_ffmpeg=True,
            )
            self.assertTrue((package_dir / "FFmpeg" / "bin" / "ffmpeg").is_file())

    def test_staging_skips_wrong_platform_ffmpeg_pair(self) -> None:
        temp_dir, root, package_dir = self._make_root()
        with temp_dir:
            (root / "FFmpeg").mkdir()
            (root / "FFmpeg" / "ffmpeg.exe").write_text("ffmpeg", encoding="utf-8")
            (root / "FFmpeg" / "ffprobe.exe").write_text("ffprobe", encoding="utf-8")

            stage_release_resources(package_dir, root=root, platform_name="linux")

            self.assertFalse((package_dir / "FFmpeg").exists())

    def test_app_staging_skips_ffmpeg_with_wrong_architecture(self) -> None:
        temp_dir, root, _ = self._make_root()
        with temp_dir:
            (root / "FFmpeg").mkdir()
            (root / "FFmpeg" / "ffmpeg").write_text("ffmpeg", encoding="utf-8")
            (root / "FFmpeg" / "ffprobe").write_text("ffprobe", encoding="utf-8")
            app_dir = root / "dist" / "QualityBound.app"
            resource_dir = app_dir / "Contents" / "Resources"
            with patch(
                "scripts.build_nuitka.binary_architectures",
                return_value={"x86_64"},
            ):
                stage_release_resources(
                    app_dir,
                    root=root,
                    platform_name="darwin",
                    resource_dir=resource_dir,
                    target_arch="arm64",
                )

            self.assertFalse((resource_dir / "FFmpeg").exists())

    def test_default_public_package_directory_is_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            expected = root / "dist" / "qualitybound"

            self.assertEqual(
                final_package_dir(root=root),
                expected,
            )
            self.assertEqual(
                build_paths(root=root).package_dir,
                expected,
            )

    def test_app_staging_uses_contents_resources(self) -> None:
        temp_dir, root, _ = self._make_root()
        with temp_dir:
            app_dir = root / "dist" / "QualityBound.app"
            app_dir.mkdir(parents=True)
            stage_release_resources(
                app_dir,
                root=root,
                platform_name="darwin",
                resource_dir=app_dir / "Contents" / "Resources",
                target_arch="arm64",
            )

            self.assertTrue(
                (app_dir / "Contents" / "Resources" / "config" / "i18n" / "en.json").is_file()
            )
            self.assertTrue((app_dir / "Contents" / "Resources" / "README.md").is_file())
            self.assertTrue((app_dir / "Contents" / "Resources" / "LICENSE").is_file())
            self.assertFalse((app_dir / "Contents" / "MacOS" / "config").exists())


class NuitkaOutputDiscoveryTestCase(unittest.TestCase):
    def test_refresh_macos_dmg_uses_repository_layout_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            app = root / "dist" / "QualityBound.app"
            app.mkdir(parents=True)
            dmg = root / "dist" / "qualitybound.dmg"

            with patch("scripts.build_nuitka.subprocess.run") as run:
                refresh_macos_dmg(app, dmg)

            run.assert_called_once_with(
                [
                    str(Path(__file__).resolve().parent.parent / "scripts" / "create-dmg"),
                    str(dmg),
                    str(app),
                ],
                check=True,
                cwd=app.parent,
            )

    def test_app_and_dmg_discovery_requires_exactly_one_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            build_dir = Path(temp_dir) / "build"
            build_dir.mkdir()
            with self.assertRaisesRegex(RuntimeError, "exactly one macOS app"):
                locate_app_bundle(build_dir)
            app = build_dir / "QualityBound.app"
            app.mkdir()
            self.assertEqual(locate_app_bundle(build_dir), app.resolve())
            (build_dir / "Other.app").mkdir()
            with self.assertRaisesRegex(RuntimeError, "exactly one macOS app"):
                locate_app_bundle(build_dir)

            dmg = build_dir / "QualityBound.dmg"
            dmg.write_bytes(b"dmg")
            self.assertEqual(locate_dmg(build_dir), dmg.resolve())
            (build_dir / "Other.dmg").write_bytes(b"dmg")
            with self.assertRaisesRegex(RuntimeError, "exactly one macOS DMG"):
                locate_dmg(build_dir)

    def test_clean_removes_app_and_dmg_without_removing_other_dist_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            dist = root / "dist"
            app = dist / "QualityBound.app"
            app.mkdir(parents=True)
            (dist / "qualitybound.dmg").write_bytes(b"dmg")
            keep = dist / "keep.txt"
            keep.write_text("keep", encoding="utf-8")

            clean_generated_paths(root, app, output_dir=dist)

            self.assertFalse(app.exists())
            self.assertFalse((dist / "qualitybound.dmg").exists())
            self.assertTrue(keep.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
