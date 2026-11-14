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


def test_catalogue_keys_and_placeholder_contracts():
    from pipelines.gpu_prices.i18n import load_catalogs, placeholders
    catalogs = load_catalogs()
    english = catalogs['en'].messages
    assert len(catalogs) == 8
    for lang, catalog in catalogs.items():
        assert catalog.messages.keys() == english.keys(), lang
        for key, value in catalog.messages.items():
            assert placeholders(value) == placeholders(english[key]), (lang, key)
            assert '\ufffd' not in value, (lang, key)


def test_region_policy_is_not_replaced_by_statistics_copy():
    # The old translations repeated basis and lost the access restrictions,
    # unspecified-region rule and treatment of unrecognized identifiers.
    for lang in LANGS:
        messages = _load(lang)
        assert messages['method_regions'] != messages['basis'], lang
        assert 'Jio' in messages['method_regions'], lang


def test_english_fallbacks_are_only_native_words_names_and_units():
    # These are also valid local words or technical names, never untranslated
    # sentences. A new English sentence in another locale must be translated.
    common = {'page_title', 'chart_point', 'sku', 'vcpu', 'gpu_1', 'spec_vram', 'spec_fp16', 'spec_bf16'}
    allowed = {
        'zh-Hans': set(), 'ja': {'spot'}, 'ko': set(),
        'de': {'source_name', 'spot', 'region', 'minimum', 'median', 'maximum', 'ram', 'median_n', 'partner_heading'},
        'fr': {'spot', 'minimum', 'maximum', 'ram', 'source', 'scope_instance', 'billing_instance_minute', 'interruptible', 'spec_conditions'},
        'es': {'spot', 'ram'}, 'pt-BR': {'spot', 'ram'},
    }
    english = _load('en')
    for lang in LANGS:
        if lang == 'en':
            continue
        identical = {key for key, value in _load(lang).items() if value == english[key]}
        assert identical <= common | allowed[lang], (lang, identical - common - allowed[lang])


def test_workload_fact_check_terms_and_developer_placeholders():
    for lang in LANGS:
        m=_load(lang)
        for key in ('vendor_context','vendor_bf16','vendor_4bit','vendor_mistral'):
            assert '{developer}' in m['workload_'+key], (lang,key)
        assert '1024³' in m['workload_vendor_context']
        note=m['workload_vendor_mistral']
        assert all(v in note for v in ('32768','8192','1/4','20 GB','5 GB'))
        labels=[m[f'workload_source_deepseek_qwen_{n}b'] for n in (7,14,32)]
        assert len(set(labels+[m['workload_source_config']]))==4
    zh=_load('zh-Hans');de=_load('de')
    assert '4-bit 量化' in zh['workload_vendor_4bit']
    assert '批量 1' in zh['workload_vendor_context'] and '批量大小' in zh['workload_vendor_mistral']
    assert '四位' not in zh['workload_vendor_4bit']+zh['workload_vendor_mistral']
    assert '{source}列出' in zh['workload_vendor_mistral']
    assert '{source}は' in _load('ja')['workload_vendor_mistral']
    assert 'Hidden-Size' in de['workload_exact_inputs'] and 'Normalisierungsschichten' in de['workload_exact_inputs']
    assert re.search(r'\{context\} Tokens\b', de['workload_exact_inputs'])
    assert not re.search(r'\bTokenss+\b', de['workload_exact_inputs'])
    assert 'Go décimaux' in _load('fr')['workload_vendor_context']
    for lang in ('fr','es','pt-BR'):
        assert '4 bits' in _load(lang)['workload_vendor_4bit']
    assert 'desses valores' in _load('pt-BR')['workload_vendor_mistral']
