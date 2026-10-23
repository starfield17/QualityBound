from __future__ import annotations

import ast
import string
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fields(template: str) -> frozenset[str]:
    fields: set[str] = set()
    for _, field, spec, _ in string.Formatter().parse(template):
        if field is not None:
            if not field or field.isdecimal():
                raise ValueError('use named format fields')
            fields.add(field)
            fields.update(_fields(spec))
    return frozenset(fields)


def _validate_text(node: ast.Call) -> None:
    if node.args or {kw.arg for kw in node.keywords} != {'en', 'zh_cn'} or len(node.keywords) != 2:
        raise ValueError('Text requires exactly en= and zh_cn=')
    values: dict[str, str] = {}
    for kw in node.keywords:
        if not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str):
            raise ValueError('translations must be literal strings; format after selecting the language')
        if not kw.value.value.strip():
            raise ValueError('translation must not be empty')
        assert kw.arg is not None
        values[kw.arg] = kw.value.value
    if _fields(values['en']) != _fields(values['zh_cn']):
        raise ValueError('translation placeholder mismatch')


class InlineTranslationValidationTestCase(unittest.TestCase):
    def test_every_application_text_is_complete_and_has_matching_placeholders(self) -> None:
        errors: list[str] = []
        count = 0
        paths = [REPO_ROOT / 'main.py']
        for package in ('core', 'cli', 'gui'):
            paths.extend(sorted((REPO_ROOT / package).rglob('*.py')))
        for path in paths:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Text':
                    count += 1
                    try:
                        _validate_text(node)
                    except ValueError as exc:
                        errors.append(f'{path.relative_to(REPO_ROOT)}:{node.lineno}: {exc}')
        self.assertGreater(count, 0)
        self.assertEqual(errors, [])

    def test_validation_rejects_missing_language_mismatched_fields_and_invalid_templates(self) -> None:
        for source in (
            'Text(en="Ready")',
            'Text(en="Ready", zh_cn="")',
            'Text(en="{count}", zh_cn="{total}")',
            'Text(en="{count", zh_cn="{count}")',
            'Text(en="{}", zh_cn="{}")',
            'Text(en="Ready", zh_cn=translated_value)',
            'Text(en=f"{count}", zh_cn="{count}")',
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                node = ast.parse(source, mode='eval').body
                assert isinstance(node, ast.Call)
                _validate_text(node)

    def test_validation_handles_literal_braces_repeated_fields_and_nested_specs(self) -> None:
        node = ast.parse('Text(en="{{{value:{width}.1f}}} {value}", zh_cn="{value:{width}.1f}")', mode='eval').body
        assert isinstance(node, ast.Call)
        _validate_text(node)
        self.assertEqual(_fields('{{literal}} {value:{width}.1f} {value}'), frozenset({'value', 'width'}))

    def test_key_lookup_and_resource_loading_are_removed(self) -> None:
        self.assertFalse((REPO_ROOT / 'config/i18n').exists())
        for package in ('core', 'cli', 'gui'):
            for path in (REPO_ROOT / package).rglob('*.py'):
                source = path.read_text(encoding='utf-8')
                self.assertNotIn('TranslationCatalog', source, str(path))
                self.assertNotIn('get_translator', source, str(path))
                for node in ast.walk(ast.parse(source)):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 't' and node.args:
                        self.assertNotIsInstance(node.args[0], ast.Constant, f'{path}:{node.lineno}')


if __name__ == '__main__':
    unittest.main()
