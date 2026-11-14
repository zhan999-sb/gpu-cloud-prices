"""b16 search landing pages and adversarial independent SEO/data audit."""
from pathlib import Path
import json
import re
import shutil
import struct
import xml.etree.ElementTree as ET
import zlib

from functools import partial

import pytest

from pipelines.gpu_prices.audit import AuditError, Page, audit_site
from pipelines.gpu_prices.build_site import build_site, model_slugs
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore

# This module tests its feature in all eight languages. The complete workload
# matrix and its independent audit remain in test_gpu_workloads.py.
build_site = partial(build_site, include_workloads=False)
audit_site = partial(audit_site, require_workloads=False)
from pipelines.gpu_prices.search_specs import PAIRS

DAY='2026-11-12'
STAMP=DAY+'T17:43:29.976595Z'
ORIGIN='https://gpu.example.test'


def quote(model='H100 SXM 80GB', price=2, sku='h100', **changes):
    values=dict(provider='azure',sku=sku,gpu_model=model,gpu_count=1,vcpu=8,ram_gb=32,
        region='eastus',price_type='on_demand',currency='USD',price_per_hour=price,
        price_per_gpu_hour=price,source_url='https://example.test/quote',collected_at=STAMP,
        effective_start_date='2026-01-01T00:00:00Z')
    values.update(changes)
    return Offer(**values)


@pytest.fixture(scope='module')
def search_site(tmp_path_factory):
    folder=tmp_path_factory.mktemp('search')
    store=PriceStore(':memory:')
    names=['H100 SXM 80GB','A100 SXM4 80GB','L40S 48GB','RTX 4090 24GB','RTX 5090 32GB','H200 SXM5 141GB']
    rows=[quote(m,price=p,sku=m+'-'+str(i),price_type='spot' if i==0 else 'on_demand') for m in names for i,p in enumerate((2,4,6))]
    rows += [quote(price=p,sku='nebius-'+str(i),provider='nebius',tier='B',export_allowed=False) for i,p in enumerate((1,5,9))]
    rows += [quote(price=10,sku='history',collected_at='2026-11-01T09:00:00Z'),
             quote(price=100,sku='old',collected_at='2026-10-01T09:00:00Z'),
             quote(price=.01,sku='future',provider='linode',collected_at='2026-11-20T09:00:00Z'),
             quote('Future-only GPU',price=.01,sku='future-model',provider='linode',collected_at='2026-11-20T09:00:00Z')]
    rows += [quote('RTX 4090 24GB',price=10+i,sku='desktop-'+str(i)) for i in range(26)]
    # Five current rows but only two eligible: raw count cannot bypass noindex.
    rows += [quote('T4 16GB',price=2,sku='thin-'+str(i),is_from_price=i>1) for i in range(5)]
    # These variants must not join the desktop comparison.
    rows += [quote('RTX 4090 Laptop GPU 16GB',price=.1,sku='laptop'),
             quote('RTX 5090 D 32GB',price=.1,sku='variant')]
    # B-only model cannot emit Dataset JSON-LD or an export.
    rows += [quote('B-only GPU',price=3+i,sku='b-only-'+str(i),provider='nebius',tier='B',export_allowed=False) for i in range(3)]
    store.upsert(rows)
    result=build_site(store,folder,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    yield store,folder,result
    store.close()


def test_search_copy_and_a_only_datasets_in_all_languages(search_site):
    store,folder,result=search_site
    catalogs=load_catalogs()
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        page=Page((folder/lang.prefix/'h100-sxm-80gb/index.html').read_text(encoding='utf-8'))
        assert page.h1==[tr.text('seo_h100_title',model='H100 SXM 80GB')]
        assert page.search_values['intro']==tr.text('search_intro',minimum='$1.00',median='$4.50',offers=6,providers=2,demand=5,spot=1)
        # Nov 1 demand minimum 10 -> Nov 12 minimum 1. No Oct/future leakage.
        assert '-90.00%' in page.search_values['trend']
        assert '2026-10-01' not in page.search_values['trend'] and '2026-11-20' not in page.search_values['trend']
        faq=next(d for d in page.structured if d['@type']=='FAQPage')
        assert len(page.faqs)==len(faq['mainEntity'])==4
        assert [q['name'] for q in faq['mainEntity']]==page.faq_questions
        dataset=next(d for d in page.structured if d['@type']=='Dataset')
        assert dataset['isBasedOn']==['https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices']
        assert '$2.00' in dataset['description'] and '$1.00' not in dataset['description']
        b=Page((folder/lang.prefix/'b-only-gpu/index.html').read_text(encoding='utf-8'))
        assert not any(d['@type']=='Dataset' for d in b.structured)
    assert not (folder/'data/b-only-gpu.json').exists()
    assert not (folder/'future-only-gpu').exists() and not (folder/'data/future-only-gpu.json').exists()
    exported=json.loads((folder/'data/h100-sxm-80gb.json').read_text(encoding='utf-8'))
    assert all(r['tier']=='A' and r['export_allowed'] for r in exported['offers'])


def test_all_model_pages_small_complete_and_thin_guard_uses_eligibility(search_site):
    store,folder,result=search_site
    for lang in LANGUAGES:
        root=folder/lang.prefix/'rtx-4090-24gb'
        pages=list(root.rglob('*.html'))
        assert len(pages)==3 and all(p.stat().st_size<300_000 for p in pages)
        cards=[c for path in pages for c in Page(path.read_text(encoding='utf-8')).cards]
        assert len(cards)==29 and len({c['attrs']['data-offer'] for c in cards})==29
        assert all(Page(p.read_text(encoding='utf-8')).metadata.get('robots')=='noindex' for p in pages if p.parent!=root)
        thin=Page((folder/lang.prefix/'t4-16gb/index.html').read_text(encoding='utf-8'))
        assert len(thin.cards)==5 and thin.metadata['robots']=='noindex'
        locations={n.text for n in ET.parse(folder/('sitemap-'+lang.code+'.xml')).iter('{http://www.sitemaps.org/schemas/sitemap/0.9}loc')}
        assert ORIGIN+lang.path('/t4-16gb/') not in locations
        assert ORIGIN+lang.path('/rtx-4090-24gb/') in locations
    assert result['audit']['indexed_pages']+result['audit']['noindex_pages']==result['html_pages']


def test_provider_comparison_tables_and_no_variant_inference(search_site):
    _,folder,result=search_site
    provider=Page((folder/'providers/nebius/index.html').read_text(encoding='utf-8'))
    assert provider.search_values['provider-counts']=='Current listings: 6; eligible offers: 6; models: 2.'
    assert provider.search_values['count:H100 SXM 80GB']=='3'
    pair=Page((folder/'compare/rtx-4090-vs-rtx-5090/index.html').read_text(encoding='utf-8'))
    assert not any('Laptop' in p or '5090 D' in p for p,_,_ in pair.values)
    values={p:v for p,_,v in pair.values}
    assert values['/specs/RTX 4090 24GB/fp16']=='330.4'
    assert values['/specs/RTX 5090 32GB/bandwidth']=='1792'
    assert 'FP32 accumulation' in pair.text
    assert all((folder/('compare/'+slug+'/index.html')).exists() for slug in PAIRS)


@pytest.mark.parametrize('damage',[
    'intro','trend','hidden-faq','faq-question','provider-count','provider-min','spec','spec-source',
    'canonical','hreflang','robots','thin-index','sitemap-lastmod','language-sitemap',
    'continuation-card','model-link','png-model','b-dataset','b-extra-dataset',
])
def test_independent_audit_rejects_search_corruption(search_site,tmp_path,damage):
    store,source,_=search_site
    out=tmp_path/'site';shutil.copytree(source,out,copy_function=shutil.copy2)
    path=out/'h100-sxm-80gb/index.html'
    if damage in {'provider-count','provider-min'}:path=out/'providers/nebius/index.html'
    if damage in {'spec','spec-source'}:path=out/'compare/rtx-4090-vs-rtx-5090/index.html'
    if damage=='thin-index':path=out/'t4-16gb/index.html'
    if damage=='sitemap-lastmod':path=out/'sitemap.xml'
    if damage=='language-sitemap':path=out/'sitemap-zh-Hans.xml'
    if damage=='robots':path=out/'robots.txt'
    if damage=='continuation-card':path=out/'rtx-4090-24gb/offers/2/index.html'
    if damage in {'b-dataset','b-extra-dataset'}:path=out/'b-only-gpu/index.html'
    if damage=='png-model':
        # Valid PNG from another model still has valid CRCs and dimensions.
        (out/'social/h100-sxm-80gb.png').write_bytes((out/'social/rtx-4090-24gb.png').read_bytes())
    else:
        text=path.read_text(encoding='utf-8');original=text
        if damage=='intro':text=text.replace('minimum $1.00, median $4.50','minimum $0.10, median $4.50',1)
        elif damage=='trend':text=text.replace('-90.00%','-99.00%',1)
        elif damage=='hidden-faq':text=text.replace('data-faq="0"','hidden data-faq="0"',1)
        elif damage=='faq-question':
            question=load_catalogs()['en'].text('faq_cheapest_q',model='H100 SXM 80GB')
            text=text.replace(question,'Incorrect FAQ question',1)
        elif damage=='provider-count':text=text.replace('Current listings: 6','Current listings: 60',1)
        elif damage=='provider-min':text=text.replace('data-format="money">$1.00','data-format="money">$0.10',1)
        elif damage=='spec':text=text.replace('data-format="number">1792','data-format="number">1793',1)
        elif damage=='spec-source':text=text.replace('https://images.nvidia.com/aem-dam/Solutions/geforce/ada/nvidia-ada-gpu-architecture.pdf','https://example.test/spec',1)
        elif damage=='canonical':text=text.replace('rel="canonical" href="'+ORIGIN,'rel="canonical" href="https://wrong.test',1)
        elif damage=='hreflang':text=text.replace('hreflang="x-default"','hreflang="xx"',1)
        elif damage=='robots':text=text.replace('Allow: /','Disallow: /',1)
        elif damage=='thin-index':text=text.replace('<meta name="robots" content="noindex"/>','',1)
        elif damage=='sitemap-lastmod':text=text.replace('<lastmod>2026-11-12</lastmod>','<lastmod>2026-11-13</lastmod>',1)
        elif damage=='language-sitemap':text=text.replace(ORIGIN+'/zh/',ORIGIN+'/',1)
        elif damage=='continuation-card':text=text.replace('data-offer="','data-missing="',1)
        elif damage=='model-link':text=text.replace('href="/providers/azure/"','href="/providers/missing/"')
        elif damage=='b-dataset':text=text.replace('"@type": "WebPage"','"@type": "Dataset"',1)
        elif damage=='b-extra-dataset':text=text.replace('</head>','<script type="application/ld+json">{"@type":"Dataset","name":"unpermitted"}</script></head>',1)
        assert text!=original,damage
        path.write_text(text,encoding='utf-8')
    with pytest.raises(AuditError):audit_site(store.connection,out)


def test_new_translations_have_no_encoding_corruption_or_banned_wording():
    catalogs=load_catalogs();reference=catalogs['en'].messages
    keys={k for k in reference if k.startswith(('search_','spec_','provider_','compare_','offer_page'))}|{'eligible_offers','breadcrumbs','reference_specs','providers_heading','comparisons_heading'}
    for code,tr in catalogs.items():
        assert keys<=tr.messages.keys()
        assert all('\ufffd' not in tr.messages[k] and '???' not in tr.messages[k] for k in keys)
        if code!='en':assert all(tr.messages[k]!=reference[k] for k in ('search_title','search_intro','search_trend','provider_intro','compare_intro','spec_note'))
    zh=' '.join(catalogs['zh-Hans'].messages[k] for k in keys)
    assert not any(word in zh for word in ('国内','国外','境内','海外'))


def test_cli_logging_follows_current_stderr_after_redirection_closes(capsys):
    from contextlib import redirect_stderr
    import io
    import logging
    from pipelines.gpu_prices.run import _CurrentStderr
    first=io.StringIO()
    with redirect_stderr(first):handler=logging.StreamHandler(_CurrentStderr())
    first.close()
    handler.emit(logging.LogRecord('gpu',logging.INFO,__file__,0,'safe message',(),None))
    captured=capsys.readouterr()
    assert captured.err=='safe message\n'
