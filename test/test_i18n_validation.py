import ast
import json
import unittest
from pathlib import Path

from core.i18n import LANGUAGE_NAME_KEY, _placeholders
from core.media import POST_ENCODE_ACTION_KEYS
from gui.queue.state import STATUS_KEY_BY_VALUE

REPO_ROOT = Path(__file__).resolve().parent.parent
I18N_DIR = REPO_ROOT / "config" / "i18n"
APP_PACKAGE_ROOTS = (REPO_ROOT / "core", REPO_ROOT / "cli", REPO_ROOT / "gui")

# Dynamic translation keys are not string literals at the call site and cannot be
# verified directly. They must be introduced through one of these explicit
# variables/functions, whose resolved key set is checked separately.
TOOLTIP_KEY_ALLOWLIST = {
    "",
    "gui.tooltip.backend_filtered",
    "gui.tooltip.backend_detecting",
    "gui.tooltip.encoder_preset_unavailable",
}


def _load(locale: str) -> dict[str, str]:
    with (I18N_DIR / f"{locale}.json").open(encoding="utf-8") as f:
        return json.load(f)


def _builtin_locales() -> list[str]:
    return sorted(
        path.stem for path in I18N_DIR.glob("*.json") if path.name != "en.json"
    )


def _app_python_files() -> list[Path]:
    files = [REPO_ROOT / "main.py"]
    for root in APP_PACKAGE_ROOTS:
        files.extend(sorted(root.rglob("*.py")))
    return files


def _root_name(expr: ast.expr) -> str | None:
    while isinstance(expr, ast.Attribute):
        expr = expr.value
    if isinstance(expr, ast.Name):
        return expr.id
    return None


def _translator_t_first_args(tree: ast.AST) -> list[ast.expr]:
    """First positional arg of every translator ``t(...)`` call in the tree."""
    args: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "t":
            if _root_name(func.value) in {"tr", "self", "translator"}:
                args.append(node.args[0])
        elif isinstance(func, ast.Name) and func.id == "t":
            args.append(node.args[0])
    return args


class BuiltinLanguageConsistencyTestCase(unittest.TestCase):
    def test_all_builtins_have_language_name(self) -> None:
        en = _load("en")
        self.assertIsInstance(en.get(LANGUAGE_NAME_KEY), str)
        self.assertTrue(en[LANGUAGE_NAME_KEY])
        for locale in _builtin_locales():
            messages = _load(locale)
            self.assertIsInstance(messages.get(LANGUAGE_NAME_KEY), str)
            self.assertTrue(messages[LANGUAGE_NAME_KEY], msg=locale)

    def test_builtin_key_sets_match_english(self) -> None:
        en = _load("en")
        for locale in _builtin_locales():
            messages = _load(locale)
            self.assertEqual(set(en), set(messages), msg=locale)

    def test_builtin_placeholder_sets_match_english_per_key(self) -> None:
        en = _load("en")
        for locale in _builtin_locales():
            messages = _load(locale)
            for key in en:
                self.assertEqual(
                    _placeholders(en[key]),
                    _placeholders(messages[key]),
                    msg=f"{locale}:{key}",
                )


class SourceLiteralKeyTestCase(unittest.TestCase):
    def test_source_literal_keys_exist_in_english(self) -> None:
        en = _load("en")
        missing: list[tuple[Path, str]] = []
        for file in _app_python_files():
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for arg in _translator_t_first_args(tree):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value not in en:
                        missing.append((file, arg.value))
        self.assertEqual([], missing)

    def test_dynamic_keys_only_via_allowlisted_patterns(self) -> None:
        unlisted: list[tuple[Path, str]] = []
        for file in _app_python_files():
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for arg in _translator_t_first_args(tree):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    continue
                if isinstance(arg, ast.Name) and arg.id == "tooltip_key":
                    continue
                if (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id in {
                        "status_key",
                        "post_encode_action_key",
                        "progress_stage_key",
                        "progress_state_key",
                    }
                ):
                    continue
                unlisted.append((file, ast.unparse(arg)))
        self.assertEqual([], unlisted)

    def test_status_key_values_exist_in_english(self) -> None:
        en = _load("en")
        for value in STATUS_KEY_BY_VALUE.values():
            self.assertIn(value, en)

    def test_post_encode_action_key_values_exist_in_english(self) -> None:
        en = _load("en")
        for value in POST_ENCODE_ACTION_KEYS.values():
            self.assertIn(value, en)

    def test_tooltip_key_literals_match_allowlist(self) -> None:
        en = _load("en")
        literals: set[str] = set()
        for file in _app_python_files():
            tree = ast.parse(file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Name) and target.id == "tooltip_key"
                    for target in node.targets
                ):
                    continue
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        literals.add(sub.value)
        self.assertTrue(literals)
        self.assertLessEqual(literals, TOOLTIP_KEY_ALLOWLIST)
        for literal in literals:
            if literal:
                self.assertIn(literal, en)


if __name__ == "__main__":
    unittest.main(verbosity=2)
