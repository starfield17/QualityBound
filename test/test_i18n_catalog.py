import json
import tempfile
import unittest
from pathlib import Path

from core.i18n import LANGUAGE_NAME_KEY, LanguageInfo, TranslationCatalog, TranslationError


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


EN_BASE = {
    LANGUAGE_NAME_KEY: "English",
    "app.title": "QualityBound",
    "app.window": "Window {count}",
    "app.ratio": "Ratio {value:.1f}",
}


class TranslationCatalogTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _bundle(self, files: dict[str, dict]) -> Path:
        bundle = self.tmp / "bundle" / "config" / "i18n"
        for name, data in files.items():
            _write_json(bundle / name, data)
        return bundle

    def test_english_only_is_baseline(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        catalog = TranslationCatalog(bundle_dir=bundle)
        self.assertEqual([LanguageInfo("en", "English")], catalog.languages())
        tr = catalog.translator("en")
        self.assertEqual("en", tr.language)
        self.assertEqual("QualityBound", tr.t("app.title"))

    def test_builtin_discovery_and_stable_sort_english_first(self) -> None:
        bundle = self._bundle(
            {
                "en.json": EN_BASE,
                "zh_cn.json": {**EN_BASE, LANGUAGE_NAME_KEY: "简体中文", "app.title": "QualityBound"},
                "de.json": {**EN_BASE, LANGUAGE_NAME_KEY: "Deutsch", "app.title": "Kompressor"},
            }
        )
        catalog = TranslationCatalog(bundle_dir=bundle)
        self.assertEqual({"en", "de", "zh_cn"}, catalog.known_locales())
        self.assertEqual(
            ["en", "de", "zh_cn"],
            [info.code for info in catalog.languages()],
        )

    def test_user_only_locale_is_discovered(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(
            translations / "fr.json",
            {LANGUAGE_NAME_KEY: "Français", "app.title": "Compresseur vidéo"},
        )
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        self.assertTrue(catalog.has_locale("fr"))
        self.assertEqual(["en", "fr"], [info.code for info in catalog.languages()])
        tr = catalog.translator("fr")
        self.assertEqual("fr", tr.language)
        self.assertEqual("Compresseur vidéo", tr.t("app.title"))

    def test_translator_falls_back_to_english_for_missing_keys(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(translations / "fr.json", {LANGUAGE_NAME_KEY: "Français", "app.title": "Titre"})
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        tr = catalog.translator("fr")
        self.assertEqual("Titre", tr.t("app.title"))
        self.assertEqual("Window 3", tr.t("app.window", count=3))
        self.assertEqual("Ratio 1.5", tr.t("app.ratio", value=1.5))

    def test_unknown_locale_requests_fall_back_to_english(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        catalog = TranslationCatalog(bundle_dir=bundle)
        tr = catalog.translator("xx")
        self.assertEqual("en", tr.language)
        self.assertEqual("QualityBound", tr.t("app.title"))

    def test_override_unknown_key_is_skipped_with_diagnostic(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(
            translations / "fr.json",
            {LANGUAGE_NAME_KEY: "Français", "app.title": "Titre", "app.bogus": "Nope"},
        )
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        diag = catalog.diagnostics()
        self.assertEqual(1, len(diag))
        self.assertEqual("fr", diag[0].locale)
        self.assertIn("unknown key", diag[0].message)
        self.assertEqual("app.bogus", diag[0].key)
        tr = catalog.translator("fr")
        self.assertEqual("Titre", tr.t("app.title"))

    def test_override_non_string_value_is_skipped_with_diagnostic(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(
            translations / "fr.json",
            {LANGUAGE_NAME_KEY: "Français", "app.title": 123},
        )
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        diag = catalog.diagnostics()
        self.assertEqual(1, len(diag))
        self.assertIn("non-string value", diag[0].message)
        self.assertEqual("app.title", diag[0].key)

    def test_override_placeholder_mismatch_is_skipped_with_diagnostic(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(
            translations / "fr.json",
            {LANGUAGE_NAME_KEY: "Français", "app.window": "Fenêtre {n}"},
        )
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        diag = catalog.diagnostics()
        self.assertEqual(1, len(diag))
        self.assertIn("placeholder mismatch", diag[0].message)
        self.assertEqual("app.window", diag[0].key)

    def test_override_corrupt_json_skips_whole_file(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        translations.mkdir(parents=True)
        (translations / "fr.json").write_text("{not json", encoding="utf-8")
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        diag = catalog.diagnostics()
        self.assertEqual(1, len(diag))
        self.assertIn("corrupt JSON", diag[0].message)
        self.assertFalse(catalog.has_locale("fr"))

    def test_override_non_object_root_skips_whole_file(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(translations / "fr.json", [1, 2, 3])
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        diag = catalog.diagnostics()
        self.assertEqual(1, len(diag))
        self.assertIn("root is not an object", diag[0].message)
        self.assertFalse(catalog.has_locale("fr"))

    def test_override_missing_language_name_skips_whole_file(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(translations / "fr.json", {"app.title": "Titre"})
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        diag = catalog.diagnostics()
        self.assertEqual(1, len(diag))
        self.assertIn("missing language.name", diag[0].message)
        self.assertFalse(catalog.has_locale("fr"))

    def test_override_invalid_filename_is_ignored_with_diagnostic(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(translations / "Bad.Name.json", {LANGUAGE_NAME_KEY: "Nope"})
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        diag = catalog.diagnostics()
        self.assertEqual(1, len(diag))
        self.assertIn("invalid filename", diag[0].message)
        self.assertFalse(catalog.has_locale("Bad.Name"))

    def test_override_files_only_read_from_translations_dir(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        # A stale file in some legacy runtime config/i18n is never consulted.
        legacy = self.tmp / "old-runtime" / "config" / "i18n"
        _write_json(legacy / "de.json", {LANGUAGE_NAME_KEY: "Deutsch", "app.title": "Alt"})
        translations = self.tmp / "translations"
        translations.mkdir(parents=True)
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        self.assertFalse(catalog.has_locale("de"))
        self.assertEqual(["en"], [info.code for info in catalog.languages()])
        self.assertEqual([], catalog.diagnostics())

    def test_partial_file_in_bundle_dir_is_treated_as_builtin_and_fails(self) -> None:
        bundle = self._bundle(
            {"en.json": EN_BASE, "de.json": {LANGUAGE_NAME_KEY: "Deutsch", "app.title": "x"}}
        )
        with self.assertRaises(TranslationError):
            TranslationCatalog(bundle_dir=bundle)

    def test_builtin_missing_language_name_is_fatal(self) -> None:
        bad = dict(EN_BASE)
        del bad[LANGUAGE_NAME_KEY]
        bundle = self._bundle({"en.json": bad})
        with self.assertRaises(TranslationError) as ctx:
            TranslationCatalog(bundle_dir=bundle)
        self.assertIn(LANGUAGE_NAME_KEY, str(ctx.exception))

    def test_builtin_key_set_mismatch_is_fatal(self) -> None:
        de = {**EN_BASE, LANGUAGE_NAME_KEY: "Deutsch"}
        del de["app.window"]
        bundle = self._bundle({"en.json": EN_BASE, "de.json": de})
        with self.assertRaises(TranslationError) as ctx:
            TranslationCatalog(bundle_dir=bundle)
        self.assertIn("key set differs", str(ctx.exception))

    def test_builtin_placeholder_mismatch_is_fatal(self) -> None:
        de = {**EN_BASE, LANGUAGE_NAME_KEY: "Deutsch", "app.window": "Fenster {n}"}
        bundle = self._bundle({"en.json": EN_BASE, "de.json": de})
        with self.assertRaises(TranslationError) as ctx:
            TranslationCatalog(bundle_dir=bundle)
        self.assertIn("placeholder mismatch", str(ctx.exception))

    def test_builtin_corrupt_json_is_fatal(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        (bundle / "de.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(TranslationError):
            TranslationCatalog(bundle_dir=bundle)

    def test_default_translations_dir_is_next_to_bundle(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = bundle.parent / "translations"
        _write_json(translations / "fr.json", {LANGUAGE_NAME_KEY: "Français"})
        catalog = TranslationCatalog(bundle_dir=bundle)
        self.assertTrue(catalog.has_locale("fr"))
        self.assertEqual("Français", catalog.languages()[-1].name)

    def test_diagnostics_returns_a_copy(self) -> None:
        bundle = self._bundle({"en.json": EN_BASE})
        translations = self.tmp / "translations"
        _write_json(translations / "fr.json", {LANGUAGE_NAME_KEY: "Français", "app.bogus": "x"})
        catalog = TranslationCatalog(bundle_dir=bundle, translations_dir=translations)
        first = catalog.diagnostics()
        first.clear()
        self.assertEqual(1, len(catalog.diagnostics()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
