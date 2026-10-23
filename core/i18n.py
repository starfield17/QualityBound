"""Code-owned multilingual text, selected and formatted at presentation time.

Text declarations live beside their callers. There is no catalog, registration,
resource discovery, or process-wide mutable language selection.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class Text:
    en: str
    zh_cn: str
    ja: str


@dataclass(frozen=True, slots=True)
class LanguageInfo:
    code: str
    name: str


LANGUAGES = (
    LanguageInfo('en', 'English'),
    LanguageInfo('zh_cn', '简体中文'),
    LanguageInfo('ja', '日本語'),
)


class Translator:
    def __init__(self, language: str = 'en') -> None:
        self.language = language if any(info.code == language for info in LANGUAGES) else 'en'

    def t(self, text: Text, **kwargs: object) -> str:
        if self.language == 'zh_cn':
            template = text.zh_cn
        elif self.language == 'ja':
            template = text.ja
        else:
            template = text.en
        try:
            return template.format(**kwargs)
        except Exception:
            # Preserve visible text if a caller supplies incomplete/invalid values.
            return template

