"""Guard the site translations against lossy-encoding damage.

A text file written through a non-UTF-8 code page silently turns every
non-ASCII character into '?'. That once shipped to the live site, so any
'?' inside a word, '??' runs, or CJK values without CJK script now fail.
"""
import json
import re
from pathlib import Path

import pytest

I18N = Path(__file__).resolve().parents[1] / "pipelines" / "gpu_prices" / "i18n"
LANGS = sorted(path.stem for path in I18N.glob("*.json"))
DAMAGE = re.compile(r"\?\?|[^\W\d_]\?[^\W\d_]|(?:^|[\s(\"“'«])\?[^\W\d_]")
NATIVE = {
    "zh-Hans": re.compile(r"[一-鿿]"),
    "ja": re.compile(r"[぀-ヿ一-鿿]"),
    "ko": re.compile(r"[가-힯]"),
}


def _load(lang):
    return json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))


def _letters(value):
    return re.sub(r"\{[a-z_]+\}|[\W\d_]", "", value)


def test_catalogues_present():
    assert {"en", "zh-Hans", "ja", "ko"} <= set(LANGS)


@pytest.mark.parametrize("lang", LANGS)
def test_no_replacement_character_damage(lang):
    damaged = sorted(key for key, value in _load(lang).items()
                     if isinstance(value, str) and DAMAGE.search(value))
    assert not damaged, f"{lang}: '?' replacement damage in {damaged}"


@pytest.mark.parametrize("lang", sorted(NATIVE))
def test_cjk_catalogues_use_native_script(lang):
    english, native = _load("en"), NATIVE[lang]
    missing = sorted(
        key for key, value in _load(lang).items()
        # Technical terms kept in English (only punctuation localised) are fine.
        if isinstance(value, str) and _letters(value) != _letters(english.get(key, ""))
        and len(_letters(value)) > 12 and not native.search(value))
    assert not missing, f"{lang}: translated values without {lang} script: {missing}"
