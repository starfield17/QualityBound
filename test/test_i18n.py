from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from core.i18n import LANGUAGES, LanguageInfo, Text, Translator


class InlineTranslationTestCase(unittest.TestCase):
    def test_supported_languages_are_stable_and_have_native_names(self) -> None:
        self.assertEqual(
            LANGUAGES,
            (
                LanguageInfo('en', 'English'),
                LanguageInfo('zh_cn', '简体中文'),
                LanguageInfo('ja', '日本語'),
            ),
        )

    def test_translation_selects_language_without_resource_files(self) -> None:
        text = Text(en='Dismiss', zh_cn='关闭', ja='閉じる')
        with patch('pathlib.Path.read_text', side_effect=AssertionError('translation must not read files')):
            self.assertEqual(Translator('en').t(text), 'Dismiss')
            self.assertEqual(Translator('zh_cn').t(text), '关闭')
            self.assertEqual(Translator('ja').t(text), '閉じる')

    def test_unknown_saved_language_falls_back_to_english(self) -> None:
        tr = Translator('fr')
        self.assertEqual(tr.language, 'en')
        self.assertEqual(tr.t(Text(en='Dismiss', zh_cn='关闭', ja='閉じる')), 'Dismiss')

    def test_language_is_per_translator_and_text_is_not_pretranslated(self) -> None:
        text = Text(en='Ready', zh_cn='就绪', ja='準備完了')
        english, chinese, japanese = Translator('en'), Translator('zh_cn'), Translator('ja')
        self.assertEqual(chinese.t(text), '就绪')
        self.assertEqual(japanese.t(text), '準備完了')
        self.assertEqual(english.t(text), 'Ready')
        self.assertEqual(chinese.t(text), '就绪')
        self.assertEqual(japanese.t(text), '準備完了')

    def test_text_is_immutable(self) -> None:
        text = Text(en='Ready', zh_cn='就绪', ja='準備完了')
        with self.assertRaises(FrozenInstanceError):
            setattr(text, 'en', 'Changed')

    def test_formatting_uses_selected_template(self) -> None:
        text = Text(
            en='Plan items: {count}; ratio {ratio:.1f}',
            zh_cn='计划项目：{count}；比例 {ratio:.1f}',
            ja='プラン項目: {count}; 比率 {ratio:.1f}',
        )
        self.assertEqual(Translator('en').t(text, count=3, ratio=1.25), 'Plan items: 3; ratio 1.2')
        self.assertEqual(Translator('zh_cn').t(text, count=3, ratio=1.25), '计划项目：3；比例 1.2')
        self.assertEqual(Translator('ja').t(text, count=3, ratio=1.25), 'プラン項目: 3; 比率 1.2')

    def test_missing_or_invalid_format_values_leave_selected_template_visible(self) -> None:
        text = Text(en='Ratio {value:.1f}', zh_cn='比例 {value:.1f}', ja='比率 {value:.1f}')
        self.assertEqual(Translator('zh_cn').t(text), text.zh_cn)
        self.assertEqual(Translator('ja').t(text), text.ja)
        self.assertEqual(Translator('en').t(text, value='invalid'), text.en)

    def test_literal_braces_and_repeated_fields_follow_python_format(self) -> None:
        text = Text(en='{{{count}}} / {count}', zh_cn='{count} / {{{count}}}', ja='{{{count}}} / {count}')
        self.assertEqual(Translator('en').t(text, count=2), '{2} / 2')
        self.assertEqual(Translator('zh_cn').t(text, count=2), '2 / {2}')
        self.assertEqual(Translator('ja').t(text, count=2), '{2} / 2')


if __name__ == '__main__':
    unittest.main()
