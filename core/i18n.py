from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


LANGUAGE_NAME_KEY = "language.name"
# Locale filenames accept lowercase letters, digits, underscores and hyphens only.
LOCALE_FILENAME_RE = re.compile(r"^[a-z0-9_-]+\.json$")
# str.format placeholders, ignoring any format spec such as "{duration:.1f}".
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)[^}]*\}")


def _placeholders(template: str) -> frozenset[str]:
    return frozenset(PLACEHOLDER_RE.findall(template))


@dataclass(frozen=True, slots=True)
class LanguageInfo:
    """A discoverable language: its locale code and native display name."""

    code: str
    name: str


@dataclass(frozen=True, slots=True)
class TranslationDiagnostic:
    """A non-fatal problem found while loading a user translation override."""

    locale: str
    message: str
    key: str | None = None


class TranslationError(ValueError):
    """A built-in language pack failed validation; treat as a release defect."""


class TranslationCatalog:
    """Discovers languages and builds translators from builtin + override files.

    Built-in language packs are release assets: any validation failure raises
    ``TranslationError``. User override files are untrusted input: malformed or
    invalid entries are skipped individually (or as a whole file) and recorded
    as ``TranslationDiagnostic`` items without ever raising.
    """

    def __init__(
        self,
        bundle_dir: Path,
        translations_dir: Path | None = None,
    ) -> None:
        self._bundle_dir = Path(bundle_dir)
        self._translations_dir = (
            Path(translations_dir)
            if translations_dir is not None
            else Path(bundle_dir).parent / "translations"
        )
        self._builtin: dict[str, dict[str, str]] = {}
        self._overrides: dict[str, dict[str, str]] = {}
        self._names: dict[str, str] = {}
        self._diagnostics: list[TranslationDiagnostic] = []
        self._load_builtin()
        self._load_overrides()

    def _load_builtin(self) -> None:
        if not self._bundle_dir.is_dir():
            raise TranslationError(f"i18n bundle directory missing: {self._bundle_dir}")
        en_path = self._bundle_dir / "en.json"
        if not en_path.is_file():
            raise TranslationError(f"missing English baseline: {en_path}")
        en = self._read_builtin_file("en", en_path)
        self._builtin["en"] = en
        self._names["en"] = en[LANGUAGE_NAME_KEY]
        for path in sorted(self._bundle_dir.glob("*.json")):
            if path == en_path or not LOCALE_FILENAME_RE.match(path.name):
                continue
            messages = self._read_builtin_file(path.stem, path)
            self._validate_builtin_parity(path.stem, en, messages)
            self._builtin[path.stem] = messages
            self._names[path.stem] = messages[LANGUAGE_NAME_KEY]

    def _read_builtin_file(self, locale: str, path: Path) -> dict[str, str]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TranslationError(f"{locale}: corrupt JSON ({path})") from exc
        if not isinstance(data, dict):
            raise TranslationError(f"{locale}: root is not an object ({path})")
        messages: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                raise TranslationError(f"{locale}: non-string key {key!r}")
            if not isinstance(value, str):
                raise TranslationError(f"{locale}: non-string value for {key}")
            messages[key] = value
        name = messages.get(LANGUAGE_NAME_KEY)
        if not isinstance(name, str) or not name:
            raise TranslationError(f"{locale}: missing {LANGUAGE_NAME_KEY}")
        return messages

    def _validate_builtin_parity(
        self,
        locale: str,
        en: dict[str, str],
        messages: dict[str, str],
    ) -> None:
        if set(messages) != set(en):
            missing = sorted(set(en) - set(messages))
            extra = sorted(set(messages) - set(en))
            raise TranslationError(
                f"{locale}: key set differs from English "
                f"(missing={missing[:5]}, extra={extra[:5]})"
            )
        mismatched = [key for key in en if _placeholders(messages[key]) != _placeholders(en[key])]
        if mismatched:
            raise TranslationError(f"{locale}: placeholder mismatch for {mismatched[:5]}")

    def _load_overrides(self) -> None:
        if not self._translations_dir.is_dir():
            return
        baseline = self._builtin.get("en", {})
        for path in sorted(self._translations_dir.glob("*.json")):
            locale = path.stem
            if not LOCALE_FILENAME_RE.match(path.name):
                self._diagnostics.append(TranslationDiagnostic(locale, "invalid filename: skipped"))
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._diagnostics.append(TranslationDiagnostic(locale, "corrupt JSON: skipped"))
                continue
            if not isinstance(data, dict):
                self._diagnostics.append(TranslationDiagnostic(locale, "root is not an object: skipped"))
                continue
            name = data.get(LANGUAGE_NAME_KEY)
            if not isinstance(name, str) or not name:
                self._diagnostics.append(TranslationDiagnostic(locale, "missing language.name: skipped"))
                continue
            entries: dict[str, str] = {}
            for key, value in data.items():
                if key == LANGUAGE_NAME_KEY:
                    continue
                if key not in baseline:
                    self._diagnostics.append(
                        TranslationDiagnostic(locale, "unknown key: skipped", key)
                    )
                    continue
                if not isinstance(value, str):
                    self._diagnostics.append(
                        TranslationDiagnostic(locale, "non-string value: skipped", key)
                    )
                    continue
                if _placeholders(value) != _placeholders(baseline[key]):
                    self._diagnostics.append(
                        TranslationDiagnostic(locale, "placeholder mismatch: skipped", key)
                    )
                    continue
                entries[key] = value
            self._overrides[locale] = entries
            self._names[locale] = name

    def known_locales(self) -> set[str]:
        return set(self._builtin) | set(self._overrides)

    def has_locale(self, locale: str) -> bool:
        return locale in self.known_locales()

    def languages(self) -> list[LanguageInfo]:
        codes = self.known_locales()
        return [
            LanguageInfo(code, self._names[code])
            for code in sorted(codes, key=lambda c: (c != "en", c))
        ]

    def diagnostics(self) -> list[TranslationDiagnostic]:
        return list(self._diagnostics)

    def translator(self, locale: str) -> Translator:
        if not self.has_locale(locale):
            locale = "en"
        messages: dict[str, str] = dict(self._builtin.get("en", {}))
        if locale != "en":
            messages.update(self._builtin.get(locale, {}))
        messages.update(self._overrides.get(locale, {}))
        return Translator(language=locale, messages=messages)


class Translator:
    def __init__(self, language: str = "en", messages: dict[str, str] | None = None) -> None:
        self.language = language
        self.messages = messages if messages is not None else {}

    def t(self, key: str, **kwargs: object) -> str:
        # Returns the translation key itself when the template is missing or formatting fails,
        # so the UI always shows something rather than crashing.
        template = self.messages.get(key, key)
        try:
            return template.format(**kwargs)
        except Exception:
            return template


def get_translator(language: str, config_dir: Path = Path("config")) -> Translator:
    # Compatibility helper for callers that know only the app config dir.
    catalog = TranslationCatalog(
        bundle_dir=config_dir / "i18n",
        translations_dir=config_dir.parent / "translations",
    )
    return catalog.translator(language)
