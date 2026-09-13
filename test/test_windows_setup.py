from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_setup import (
    build_iscc_command,
    validate_source,
)

ROOT = Path(__file__).resolve().parent.parent


class WindowsSetupTestCase(unittest.TestCase):
    def test_script_entrypoint_imports_from_an_isolated_interpreter(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / "build_setup.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_build_command_maps_release_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            common = {
                "version": "1.6.1",
                "source_dir": root / "source",
                "output_path": root / "qualitybound-setup.exe",
                "intermediate_dir": root / "intermediate",
                "icon_path": root / "app.ico",
                "iscc_executable": "ISCC.exe",
                "iss_path": ROOT / "packaging" / "windows" / "installer.iss",
            }
            x64 = build_iscc_command(architecture="x86_64", **common)
            arm64 = build_iscc_command(architecture="arm64", **common)

        self.assertIn(
            "/DArchitecturesAllowed=x64compatible",
            x64,
        )
        self.assertIn(
            "/DArchitecturesInstallIn64BitMode=x64compatible",
            x64,
        )
        self.assertIn("/DArchitecturesAllowed=arm64", arm64)
        self.assertIn("/DArchitecturesInstallIn64BitMode=arm64", arm64)
        self.assertIn("/DReleaseVersion=1.6.1", x64)
        self.assertIn("/DVersionInfo=1.6.1.0", x64)
        self.assertFalse(any(argument.startswith("/DMyAppId=") for argument in x64))
        self.assertIn("/Qp", x64)

    def test_build_command_rejects_unknown_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "Unsupported Setup architecture"):
                build_iscc_command(
                    version="1.6.1",
                    architecture="unknown",
                    source_dir=root / "source",
                    output_path=root / "setup.exe",
                    intermediate_dir=root / "intermediate",
                    icon_path=root / "app.ico",
                )

    def test_build_command_validates_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "Invalid version"):
                build_iscc_command(
                    version="1.6.1-beta",
                    architecture="x86_64",
                    source_dir=root / "source",
                    output_path=root / "setup.exe",
                    intermediate_dir=root / "intermediate",
                    icon_path=root / "app.ico",
                )

    def test_source_validation_requires_complete_runtime_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "dist"
            icon = root / "app.ico"
            source.mkdir()
            icon.write_bytes(b"ico")
            with self.assertRaisesRegex(FileNotFoundError, "Setup source is incomplete"):
                validate_source(source, icon)

            (source / "qualitybound.exe").write_bytes(b"exe")
            ffmpeg_bin = source / "FFmpeg" / "bin"
            ffmpeg_bin.mkdir(parents=True)
            (ffmpeg_bin / "ffmpeg.exe").write_bytes(b"exe")
            (ffmpeg_bin / "ffprobe.exe").write_bytes(b"exe")
            with self.assertRaisesRegex(FileNotFoundError, "config.*i18n.*en.json"):
                validate_source(source, icon)

            i18n = source / "config" / "i18n"
            i18n.mkdir(parents=True)
            (i18n / "en.json").write_text("{}", encoding="utf-8")
            (i18n / "zh_cn.json").write_text("{}", encoding="utf-8")
            (source / "README.md").write_text("readme", encoding="utf-8")
            (source / "LICENSE").write_text("license", encoding="utf-8")
            validate_source(source, icon)


class WindowsSetupManifestTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = (ROOT / "packaging" / "windows" / "installer.iss").read_text(encoding="utf-8")

    def test_installer_is_per_user_without_uac(self) -> None:
        self.assertIn("PrivilegesRequired=lowest", self.manifest)
        self.assertNotIn("PrivilegesRequiredOverridesAllowed=admin", self.manifest)

    def test_default_directory_uses_local_app_data(self) -> None:
        self.assertIn("DefaultDirName={localappdata}\\Programs\\QualityBound", self.manifest)

    def test_start_menu_shortcut_with_working_directory(self) -> None:
        self.assertIn("[Icons]", self.manifest)
        self.assertIn(
            'Name: "{group}\\QualityBound"; Filename: "{app}\\qualitybound.exe"; WorkingDir: "{app}"',
            self.manifest,
        )

    def test_no_default_desktop_shortcut(self) -> None:
        self.assertNotIn("commondesktop", self.manifest.lower())
        self.assertNotIn('{userdesktop}', self.manifest)

    def test_no_custom_registry_section(self) -> None:
        sections = [line.strip() for line in self.manifest.splitlines() if line.strip().startswith("[")]
        self.assertNotIn("[Registry]", sections)

    def test_uninstall_display_icon_points_at_application(self) -> None:
        self.assertIn("UninstallDisplayIcon={app}\\qualitybound.exe", self.manifest)

    def test_standalone_tree_is_installed_recursively(self) -> None:
        self.assertIn('Source: "{#SourceDir}\\*"; DestDir: "{app}"', self.manifest)
        self.assertIn("recursesubdirs", self.manifest)
        self.assertIn("createallsubdirs", self.manifest)

    def test_architecture_directives_are_define_driven(self) -> None:
        self.assertIn("ArchitecturesAllowed={#ArchitecturesAllowed}", self.manifest)
        self.assertIn("ArchitecturesInstallIn64BitMode={#ArchitecturesInstallIn64BitMode}", self.manifest)

    def test_qualitybound_app_id_is_declared(self) -> None:
        self.assertIn("AppId={{F1434831-589A-59FC-9A61-AEA4B9403BFC}", self.manifest)
        self.assertNotIn("#define MyAppId", self.manifest)

    def test_previous_installer_identity_is_not_reused(self) -> None:
        self.assertNotIn("4478BF58-30E3-5232-AE83-3E33254B3385", self.manifest)

    def test_legacy_video_compressor_migration_is_not_carried_forward(self) -> None:
        self.assertNotIn("msiexec.exe", self.manifest)
        self.assertNotIn("PrepareToInstall", self.manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
