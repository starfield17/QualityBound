from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.config.paths as app_paths
from cli import cli_entry
from core.i18n import Text


class AppPathsCompiledEnvironmentTestCase(unittest.TestCase):
    def test_source_run_uses_repository_root(self) -> None:
        with patch.object(app_paths, "__compiled__", None, create=True), patch.object(sys, "frozen", False, create=True):
            self.assertFalse(app_paths.is_compiled())
            self.assertEqual(app_paths.bundle_root(), app_paths.source_root())
            self.assertEqual(app_paths.app_root(), app_paths.source_root())

    def test_sys_frozen_uses_executable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "qualitybound"
            with (
                patch.object(app_paths, "__compiled__", None, create=True),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "executable", str(executable)),
            ):
                self.assertTrue(app_paths.is_compiled())
                self.assertEqual(app_paths.bundle_root(), executable.parent.resolve())
                self.assertEqual(app_paths.app_root(), executable.parent.resolve())

    def test_icon_path_prefers_packaged_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            packaged_icon = root / "assets" / "app.svg"
            packaged_icon.parent.mkdir(parents=True)
            packaged_icon.write_text("<svg/>", encoding="utf-8")
            with patch.object(app_paths, "bundle_root", return_value=root):
                self.assertEqual(app_paths.app_icon_path(), packaged_icon.resolve())

    def test_nuitka_compiled_marker_uses_executable_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "qualitybound"
            with (
                patch.object(app_paths, "__compiled__", object(), create=True),
                patch.object(sys, "frozen", False, create=True),
                patch.object(sys, "executable", str(executable)),
            ):
                self.assertTrue(app_paths.is_compiled())
                self.assertEqual(app_paths.bundle_root(), executable.parent.resolve())
                self.assertEqual(app_paths.app_root(), executable.parent.resolve())

    def test_macos_app_bundle_uses_resources_and_application_support(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_bundle = root / "QualityBound.app"
            executable = app_bundle / "Contents" / "MacOS" / "qualitybound"
            resources = app_bundle / "Contents" / "Resources"
            resources_config = resources / "config" / "presets"
            resources_config.mkdir(parents=True)
            (resources_config / "default.json").write_text("default", encoding="utf-8")
            executable.parent.mkdir(parents=True)
            executable.write_text("executable", encoding="utf-8")
            home = root / "home"
            runtime_config = home / "Library" / "Application Support" / "QualityBound" / "config"
            runtime_config.mkdir(parents=True)
            (runtime_config / "user.json").write_text("user", encoding="utf-8")

            with (
                patch.object(app_paths, "__compiled__", object(), create=True),
                patch.object(sys, "frozen", False, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(app_paths.Path, "home", return_value=home),
            ):
                self.assertTrue(app_paths.is_macos_app_bundle())
                self.assertEqual(
                    app_paths.macos_app_bundle_path(),
                    app_bundle.resolve(),
                )
                self.assertEqual(app_paths.bundle_root(), resources.resolve())
                self.assertEqual(
                    app_paths.app_root(),
                    home / "Library" / "Application Support" / "QualityBound",
                )
                config_dir, workdir = app_paths.ensure_runtime_layout()

            self.assertEqual(config_dir, runtime_config)
            self.assertEqual(
                workdir,
                home / "Library" / "Application Support" / "QualityBound" / "workdir",
            )
            self.assertEqual((runtime_config / "presets" / "default.json").read_text(encoding="utf-8"), "default")
            self.assertEqual((runtime_config / "user.json").read_text(encoding="utf-8"), "user")
            self.assertFalse(
                (home / "Library" / "Application Support" / "QualityBound" / "translations").exists()
            )
            self.assertFalse((app_bundle / "Contents" / "MacOS" / "config").exists())
            self.assertFalse((app_bundle / "Contents" / "MacOS" / "workdir").exists())

    def test_macos_app_upgrade_preserves_legacy_translations_without_reading_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            app_bundle = root / "QualityBound.app"
            executable = app_bundle / "Contents" / "MacOS" / "qualitybound"
            bundled_i18n = app_bundle / "Contents" / "Resources" / "config" / "i18n"
            bundled_i18n.mkdir(parents=True)
            (bundled_i18n / "en.json").write_text(
                json.dumps({"existing": "Bundled", "new": "New text"}),
                encoding="utf-8",
            )
            executable.parent.mkdir(parents=True)
            executable.write_text("executable", encoding="utf-8")

            home = root / "home"
            runtime_i18n = (
                home
                / "Library"
                / "Application Support"
                / "QualityBound"
                / "config"
                / "i18n"
            )
            runtime_i18n.mkdir(parents=True)
            custom_content = json.dumps({"existing": "Customized"})
            (runtime_i18n / "en.json").write_text(custom_content, encoding="utf-8")

            with (
                patch.object(app_paths, "__compiled__", object(), create=True),
                patch.object(sys, "frozen", False, create=True),
                patch.object(sys, "executable", str(executable)),
                patch.object(app_paths.Path, "home", return_value=home),
            ):
                app_paths.ensure_runtime_layout()
                self.assertFalse((runtime_i18n.parent.parent / "translations").exists())

            # Legacy runtime config/i18n is never merged or overwritten.
            self.assertEqual(
                (runtime_i18n / "en.json").read_text(encoding="utf-8"),
                custom_content,
            )
            # The bundled i18n is not seeded into the writable config tree.
            self.assertEqual(
                json.loads((runtime_i18n / "en.json").read_text(encoding="utf-8")),
                {"existing": "Customized"},
            )

    def test_cli_selects_all_languages_without_bundle_resources(self) -> None:
        with patch("pathlib.Path.read_text", side_effect=AssertionError("must not read language packs")):
            parser = cli_entry._build_parser()
            for language, expected in (("en", "Ready"), ("zh_cn", "就绪"), ("ja", "準備完了")):
                args = parser.parse_args(["plan", "input.mp4", "--lang", language])
                with patch("cli.cli_entry.load_app_config", return_value={}):
                    tr = cli_entry._translator_for_args(args)
                self.assertEqual(tr.language, language)
                self.assertEqual(tr.t(Text(en="Ready", zh_cn="就绪", ja="準備完了")), expected)

    def test_cli_uses_saved_language_and_falls_back_from_removed_locale(self) -> None:
        for language, expected in (("zh_cn", "zh_cn"), ("fr", "en")):
            with self.subTest(language=language), patch("cli.cli_entry.load_app_config", return_value={"language": language}):
                self.assertEqual(cli_entry._translator_for_args(argparse.Namespace(lang=None)).language, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
