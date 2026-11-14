"""b20 buyer intent, Dataset discovery and independent corruption gates."""
import json
from pathlib import Path
import re
import shutil

from functools import partial

import pytest

from pipelines.gpu_prices.audit import AuditError, Page, audit_site
from pipelines.gpu_prices.build_site import build_site
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.indexnow import KEY, page_manifest
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore

# This module tests its feature in all eight languages. The complete workload
# matrix and its independent audit remain in test_gpu_workloads.py.
build_site = partial(build_site, include_workloads=False)
audit_site = partial(audit_site, require_workloads=False)

DAY='2026-11-12'
STAMP=DAY+'T12:00:00Z'
ORIGIN='https://gpu.example.test'
MODELS=['RTX 4090 24GB','H100 PCIe 80GB','H100 SXM 80GB','H100 NVL 94GB',
        'A100 PCIe 40GB','A100 SXM4 40GB','A100 80GB','A100 SXM4 80GB']


@pytest.fixture(scope='module')
def seo_site(tmp_path_factory):
    folder=tmp_path_factory.mktemp('seo')
    store=PriceStore(':memory:')
    rows=[]
    for model in MODELS:
        prices=[('on_demand','instance',16,8),('spot','instance',8,8),('on_demand','per_gpu',2,4)]
        prices += [(kind,'instance',8,8) for kind in ('interruptible','reserved','reserved_1y','reserved_3y')]
        for kind,scope,amount,count in prices:
            rows.append(Offer(provider='azure',sku=model+kind+scope,gpu_model=model,gpu_count=count,
                vcpu=8,ram_gb=32,region='eastus',price_type=kind,currency='USD',price_per_hour=amount,
                price_per_gpu_hour=amount/count if scope=='instance' else amount,price_scope=scope,
                minimum_billing_seconds=60,source_url='https://example.test/source',collected_at=STAMP))
    rows.append(Offer(provider='azure',sku='history',gpu_model='H100 SXM 80GB',gpu_count=1,
        vcpu=None,ram_gb=None,region=None,price_type='on_demand',currency='USD',price_per_hour=3,
        price_per_gpu_hour=3,source_url='https://example.test/source',collected_at='2026-11-01T12:00:00Z'))
    store.upsert(rows)
    result=build_site(store,folder,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    yield store,folder,result
    store.close()


def test_localized_titles_variants_and_spot_blocks(seo_site):
    _,folder,result=seo_site
    catalogs=load_catalogs()
    slugs=json.loads((folder/'gpu-prices-manifest.json').read_text())['model_slugs']
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        for model in MODELS:
            page=Page((folder/lang.prefix/slugs[model]/'index.html').read_text(encoding='utf-8'))
            family='4090' if model.startswith('RTX') else 'h100' if model.startswith('H100') else 'a100'
            assert page.h1==[tr.text('seo_'+family+'_title',model=model)]
            assert page.metadata['og:title']==page.h1[0]==page.metadata['twitter:title']
            assert page.search_values['buyer-intro']==tr.text('seo_'+family+'_intro')
            if family!='4090':
                assert page.search_values['variant']==tr.text('seo_variant',model=model)
                assert [kind for kind,_ in page.price_blocks]==['on_demand','spot','interruptible','reserved','reserved_1y','reserved_3y']
                assert all(c['attrs']['data-gpu-model']==model for c in page.cards)
            assert len(page.cards)==7  # No price type may disappear during grouping.
            assert all(tr.text('seo_minimum_instance') in c['roles']['minimum-instance'] for c in page.cards)
            assert any('USD $8.00/hr' in c['roles']['minimum-instance'] for c in page.cards)
            assert all(tr.text('seo_billing_floor') in c['roles']['minimum-instance'] for c in page.cards)
    assert result['audit']['status']=='passed'


def test_dataset_matches_visible_compilation_dates_and_tier_a_files(seo_site):
    _,folder,_=seo_site
    catalogs=load_catalogs()
    index=json.loads((folder/'data/latest.json').read_text(encoding='utf-8'))
    assert index['temporal_coverage']=='2026-11-01/2026-11-12'
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        page=Page((folder/lang.prefix/'data/index.html').read_text(encoding='utf-8'))
        dataset=page.structured[0]
        assert dataset['@type']=='Dataset'
        assert dataset['name']==page.h1[0]==tr.text('growth_data_title')
        assert dataset['description']==tr.text('growth_data_intro')+' '+tr.text('growth_license')
        assert tr.text('growth_license') in page.text
        assert dataset['license']=='https://creativecommons.org/licenses/by/4.0/'
        assert dataset['creator']=={'@type':'Organization','name':'GPU Price Index','url':ORIGIN+'/'}
        assert dataset['temporalCoverage']==index['temporal_coverage']
        assert dataset['temporalCoverage'] in page.text
        assert [d['contentUrl'] for d in dataset['distribution']]==[e['url'] for e in index['exports']]
        assert all(d['encodingFormat']=='application/json' for d in dataset['distribution'])
        for d in dataset['distribution']:
            export=json.loads((folder/d['contentUrl'].removeprefix(ORIGIN+'/')).read_text(encoding='utf-8'))
            assert all(o['tier']=='A' and o['export_allowed'] for o in export['offers'])


def test_rss_discovery_on_every_html_and_visible_dataset_mirror(seo_site):
    _, folder, _ = seo_site
    catalogs = load_catalogs()
    manifest = json.loads((folder/'gpu-prices-manifest.json').read_text(encoding='utf-8'))
    by_prefix = {lang.prefix: lang for lang in LANGUAGES}
    for filename in manifest['files']:
        if not filename.endswith('.html'):
            continue
        parts = filename.split('/')
        # Embeds use /embed/<locale>/<model>.html; other pages /<locale>/…
        candidate = parts[1] if parts[0] == 'embed' else parts[0]
        lang = by_prefix.get(candidate, LANGUAGES[0])
        tr = catalogs[lang.code]
        page = Page((folder/filename).read_text(encoding='utf-8'))
        assert page.rss_links == [{'rel': 'alternate', 'type': 'application/rss+xml',
            'title': tr.text('growth_feed_title'), 'href': ORIGIN+lang.path('/feed.xml')}], filename
        assert (folder/lang.prefix/'feed.xml').is_file()
        if not filename.startswith('embed/'):
            assert len([a for tag, a in page.links if tag == 'link' and 'hreflang' in a]) == 9
    for lang in LANGUAGES:
        page = Page((folder/lang.prefix/'data/index.html').read_text(encoding='utf-8'))
        assert page.mirrors == [({'data-mirror': 'huggingface',
            'href': 'https://huggingface.co/datasets/ComputeTicks/gpu-cloud-prices',
            'rel': 'noopener'}, catalogs[lang.code].text('growth_data_mirror'))]
        assert not any(d.get('@type') == 'FAQPage' for d in page.structured)


@pytest.mark.parametrize('filename,damage', [
    ('index.html', 'rss-remove'), ('zh/index.html', 'rss-locale'),
    ('providers/azure/index.html', 'rss-title'),
    ('compare/h100-vs-a100/index.html', 'rss-type'),
    ('guides/index.html', 'rss-body'), ('ja/404.html', 'rss-remove'),
    ('h100-sxm-80gb/index.html', 'rss-duplicate'),
    ('embed/ko/h100-sxm-80gb.html', 'rss-remove'),
    ('data/index.html', 'mirror-remove'), ('zh/data/index.html', 'mirror-label'),
    ('ja/data/index.html', 'mirror-url'), ('ko/data/index.html', 'mirror-hidden'),
])
def test_audit_rejects_discovery_and_mirror_corruption(seo_site, filename, damage, gpu_site_copy):
    store, folder, _ = gpu_site_copy(seo_site)
    path = folder/filename
    original = path.read_text(encoding='utf-8')
    if damage.startswith('rss-'):
        link = re.search(r'<link rel="alternate" type="application/rss\+xml"[^>]+/>', original)[0]
        changed = {'rss-remove': '', 'rss-locale': link.replace('/zh/feed.xml', '/feed.xml'),
            'rss-title': re.sub(r'title="[^"]+"', 'title="Wrong title"', link),
            'rss-type': link.replace('application/rss+xml', 'text/html'),
            'rss-body': '', 'rss-duplicate': link+link}[damage]
        raw = original.replace(link, changed, 1)
        if damage == 'rss-body':
            raw = raw.replace('</body>', link+'</body>', 1)
    else:
        link = re.search(r'<a data-mirror="huggingface"[^>]+>[^<]+</a>', original)[0]
        changed = {'mirror-remove': '', 'mirror-label': re.sub(r'>[^<]+</a>', '>Unrelated</a>', link),
            'mirror-url': link.replace('ComputeTicks', 'WrongOwner'),
            'mirror-hidden': link.replace('<a ', '<a hidden ', 1)}[damage]
        raw = original.replace(link, changed, 1)
    assert raw != original
    try:
        path.write_text(raw, encoding='utf-8')
        with pytest.raises(AuditError):
            audit_site(store.connection, folder)
    finally:
        path.write_text(original, encoding='utf-8')


@pytest.mark.parametrize('damage',[
    'name','description','license','creator','temporalCoverage','distribution','missing-dataset',
    'visible-license','visible-creator','visible-time','hidden-time','buyer-intro','variant','price-block',
    'minimum-instance','minimum-charge','hidden-minimum','block-heading','key','hashes',
])
def test_independent_audit_rejects_seo_corruption(seo_site,tmp_path,damage):
    store,source,_=seo_site
    out=tmp_path/'site';shutil.copytree(source,out,copy_function=shutil.copy2)
    path=out/'data/index.html'
    if damage in {'buyer-intro','variant','price-block','minimum-instance','minimum-charge','hidden-minimum','block-heading'}:
        path=out/'h100-sxm-80gb/index.html'
    raw=path.read_text(encoding='utf-8')
    if damage in {'name','description','license','creator','temporalCoverage','distribution','missing-dataset'}:
        def corrupt(match):
            data=json.loads(match[1])
            if data.get('@type')!='Dataset':return match[0]
            if damage=='missing-dataset':return ''
            data[damage]='false claim'
            return '<script type="application/ld+json">'+json.dumps(data)+'</script>'
        raw=re.sub(r'<script type="application/ld\+json">(.*?)</script>',corrupt,raw)
    elif damage=='visible-license':raw=raw.replace(load_catalogs()['en'].html('growth_license'),'')
    elif damage=='visible-creator':raw=raw.replace('Compilation creator:','False creator:')
    elif damage=='visible-time':raw=raw.replace('Observed temporal coverage: 2026-11-01/2026-11-12','Observed temporal coverage: 2000-01-01/2026-11-12')
    elif damage=='hidden-time':raw=raw.replace('data-growth="temporal"','data-growth="temporal" hidden')
    elif damage=='buyer-intro':raw=raw.replace('data-search="buyer-intro"','data-search="wrong-intro"')
    elif damage=='variant':raw=raw.replace('This page covers H100 SXM 80GB only.','This page covers all H100 variants.')
    elif damage=='price-block':raw=raw.replace('data-price-block="spot"','data-price-block="on_demand"')
    elif damage=='minimum-instance':raw=raw.replace('Minimum billable instance cost:','Minimum price for all GPUs:',1)
    elif damage=='minimum-charge':raw=raw.replace('data-format="minimum-charge-money">$0.133','data-format="minimum-charge-money">$9.999',1)
    elif damage=='hidden-minimum':raw=raw.replace('class="minimum-instance"','class="minimum-instance" hidden',1)
    elif damage=='block-heading':raw=raw.replace('data-price-block="spot"><h3>Spot</h3>','data-price-block="spot"><h3>On-demand</h3>')
    elif damage=='key':(out/(KEY+'.txt')).write_text('wrong',encoding='utf-8')
    elif damage=='hashes':(out/'indexnow-manifest.json').write_text('{}',encoding='utf-8')
    if damage not in {'key','hashes'}:
        assert raw!=path.read_text(encoding='utf-8'),damage
        path.write_text(raw,encoding='utf-8')
        # Keep hashes valid: semantic corruption must fail its own audit, not
        # merely the unrelated deployment hash-integrity check.
        index=json.loads((out/'indexnow-manifest.json').read_text(encoding='utf-8'))
        filename=path.relative_to(out).as_posix()
        route='/'+filename.removesuffix('index.html')
        index['pages'].update(page_manifest({filename:raw},[route],ORIGIN)['pages'])
        (out/'indexnow-manifest.json').write_text(json.dumps(index),encoding='utf-8')
    with pytest.raises(AuditError):audit_site(store.connection,out)


def test_hashes_ignore_only_build_clock_and_include_real_content(seo_site):
    _,folder,_=seo_site
    build=json.loads((folder/'indexnow-manifest.json').read_text(encoding='utf-8'))
    assert all('/offers/' not in u and '/embed/' not in u for u in build['pages'])
    raw=(folder/'rtx-4090-24gb/index.html').read_text(encoding='utf-8')
    route='/rtx-4090-24gb/'
    def hashes(content):return page_manifest({'rtx-4090-24gb/index.html':content},[route],ORIGIN)['pages']
    changed_build=re.sub(r'<time datetime="[^"]*" data-stat="/built" data-format="date">[^<]*</time>',
        '<time datetime="2026-11-13T15:00:00Z" data-stat="/built" data-format="date">2026-11-13</time>',raw)
    assert changed_build!=raw and hashes(changed_build)==hashes(raw)
    assert hashes(raw.replace('$8.00','$9.00'))!=hashes(raw)
    assert hashes(raw.replace('12:00 UTC','12:01 UTC'))!=hashes(raw)
    assert build['pages'][ORIGIN+route]==hashes(raw)[ORIGIN+route]


def test_component_unknown_allocation_and_native_currency_are_honest():
    from pipelines.gpu_prices.build_site import _offer_card
    base=dict(provider='azure',sku='test',gpu_model='H100 SXM 80GB',gpu_count=4,
        vcpu=None,ram_gb=None,region=None,price_type='on_demand',currency='EUR',price_per_hour=2,
        price_per_gpu_hour=2,price_scope='per_gpu',minimum_billing_seconds=3600,
        source_url='https://example.test/',collected_at=STAMP)
    tr=load_catalogs()['en']
    card=Page(_offer_card(Offer(**base).to_dict(),'/offers/0',tr)).cards[0]
    assert 'EUR 8/hr' in card['roles']['minimum-instance']
    for scope,count in [('per_gpu',None),('gpu_component',4)]:
        row=Offer(**{**base,'price_scope':scope,'gpu_count':count}).to_dict()
        card=Page(_offer_card(row,'/offers/0',tr)).cards[0]
        assert tr.text('seo_instance_unknown') in card['roles']['minimum-instance']
