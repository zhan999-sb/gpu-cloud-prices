"""Language routes and plain-text catalogs; no network or runtime dependencies."""

from dataclasses import dataclass
from html import escape
import json
from pathlib import Path
import re
from string import Formatter


@dataclass(frozen=True)
class Language:
    code: str
    prefix: str
    name: str

    def path(self, route: str = "/") -> str:
        return ("/" + self.prefix if self.prefix else "") + route


# The one place to configure enabled languages, URL prefixes and switcher names.
# English is the root and x-default language and must remain the first entry.
LANGUAGES = (
    Language("en", "", "English"),
    Language("zh-Hans", "zh", "简体中文"),
    Language("ja", "ja", "日本語"),
    Language("ko", "ko", "한국어"),
    Language("de", "de", "Deutsch"),
    Language("fr", "fr", "Français"),
    Language("es", "es", "Español"),
    Language("pt-BR", "pt", "Português (Brasil)"),
)
CATALOG_DIR = Path(__file__).resolve().parent


def placeholders(template: str) -> set[str]:
    """Only named fields, without attribute access, conversions or format code."""
    result = set()
    for _, name, spec, conversion in Formatter().parse(template):
        if name is not None:
            if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or spec or conversion:
                raise ValueError(f"Invalid translation placeholder: {name!r}")
            result.add(name)
    return result


@dataclass(frozen=True)
class Catalog:
    language: Language
    messages: dict[str, str]

    def text(self, key: str, **values) -> str:
        return self.messages[key].format(**values)

    def html(self, key: str, **values) -> str:
        return escape(self.text(key, **values), quote=True)

    def quantity(self, key: str, count: int) -> str:
        return self.text(key + ("_one" if count == 1 else "_other"), count=count)

    def rich(self, key: str, **trusted_html) -> str:
        """Escape catalog text, then insert HTML already escaped by the renderer."""
        return escape(self.messages[key], quote=True).format(**trusted_html)


def load_catalogs() -> dict[str, Catalog]:
    if (not LANGUAGES or LANGUAGES[0].code != "en" or LANGUAGES[0].prefix
            or len({lang.code for lang in LANGUAGES}) != len(LANGUAGES)
            or len({lang.prefix for lang in LANGUAGES}) != len(LANGUAGES)
            or any(not re.fullmatch(r"[a-z]{2}(?:-[A-Za-z]+)?", lang.code)
                   or (lang.prefix and not re.fullmatch(r"[a-z]{2}", lang.prefix))
                   for lang in LANGUAGES)):
        raise ValueError("Invalid language configuration (English must be at root)")
    result, reference = {}, None
    for lang in LANGUAGES:
        messages = json.loads((CATALOG_DIR / (lang.code + ".json")).read_text(encoding="utf-8"))
        if not isinstance(messages, dict) or not messages or any(
                not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                for key, value in messages.items()):
            raise ValueError(f"Invalid catalog: {lang.code}")
        signature = {key: placeholders(value) for key, value in messages.items()}
        if reference is None:
            reference = signature
        elif signature != reference:
            raise ValueError(f"Catalog keys/placeholders differ from English: {lang.code}")
        result[lang.code] = Catalog(lang, messages)
    return result
