"""Guide arithmetic, translation, sparse-data and adversarial audit contracts."""
from dataclasses import replace
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET

import pytest

from pipelines.gpu_prices import guides
from pipelines.gpu_prices.audit import AuditError, Page, audit_site
from pipelines.gpu_prices.build_site import build_site, model_slugs
from pipelines.gpu_prices.guide_specs import GUIDES, REFERENCE_MODELS, reference_rows
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore

DAY='2026-11-13'
STAMP=DAY+'T08:00:00Z'
ORIGIN='https://gpu.example.test'


def offer(model='H100 SXM 80GB', sku='h100', rate=2, **changes):
    args=dict(provider='azure',gpu_model=model,sku=sku,gpu_count=1,vcpu=8,ram_gb=32,
        region='eastus',price_type='on_demand',currency='USD',price_per_hour=rate,
        price_per_gpu_hour=rate,minimum_billing_seconds=60,
        source_url='https://example.test/observed',collected_at=STAMP)
    args.update(changes)
    return Offer(**args)


def population():
    rows=[]
    for i,(d,s) in enumerate(((1,.5),(2,1.8),(100,10))):
        rows.extend([offer(sku='paired-'+str(i),rate=d),
                     offer(sku='paired-'+str(i),rate=s,price_type='spot')])
    rows.extend([offer('T4 16GB','t4',.2),offer('RTX 4090 24GB','4090',.3),
        offer('L40S 48GB','l40s',.6),offer('A100 80GB','a100',2),
        offer(sku='eight',rate=16,gpu_count=8,price_per_gpu_hour=2),
        offer('RTX 4090 24GB','four-per-gpu',.4,gpu_count=4,price_scope='per_gpu'),
        offer('L40S 48GB','eight-l40s',8,gpu_count=8,price_per_gpu_hour=1),
        offer('No capacity','unknown',.01),offer('Unknown GPU 20GiB','gib',.01)])
    for i,changes in enumerate((dict(is_from_price=True),dict(tax_basis='included'),
        dict(price_scope='gpu_component'),dict(region='chinaeast'),dict(region='attatlanta1'),
        dict(currency='EUR'),dict(gpu_count=.5,gpu_fraction=.5,price_per_gpu_hour=.02),
        dict(collected_at='2026-11-12T08:00:00Z'),dict(effective_from='2026-11-14T00:00:00Z'))):
        rows.append(offer('RTX 4090 24GB','excluded-'+str(i),.01,**changes))
    # Both types exist but must never pair across geography, count or tax basis.
    rows.extend([offer('L40S 48GB','mismatch',2),
        offer('L40S 48GB','mismatch',.1,region='westus',price_type='spot'),
        offer('L40S 48GB','mismatch',.2,gpu_count=2,price_per_gpu_hour=.1,price_type='spot'),
        offer('RTX 4090 24GB','tax-mismatch',2,tax_basis='excluded'),
        offer('RTX 4090 24GB','tax-mismatch',.1,price_type='spot',tax_basis='unknown')])
    return rows


@pytest.fixture(scope='module')
def guide_site(tmp_path_factory):
    folder=tmp_path_factory.mktemp('guides')
    store=PriceStore(':memory:');store.upsert(population())
    result=build_site(store,folder,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    yield store,folder,result
    store.close()


def test_guide_live_calculations_and_normalization(guide_site):
    _,out,result=guide_site
    choose=Page((out/'guides/choose-gpu-cloud-llm-inference/index.html').read_text(encoding='utf-8'))
    assert choose.table_rows[:3]==[['Llama 2 7B','7','14','3.5'],['Llama 2 13B','13','26','6.5'],['Llama 2 70B','70','140','35']]
    assert choose.table_rows[3][0]=='16 GB – < 24 GB'
    assert choose.table_rows[-1][0]=='80 GB+'
    spot=Page((out/'guides/spot-vs-on-demand/index.html').read_text(encoding='utf-8'))
    assert spot.table_rows==[['H100 SXM 80GB','3','$2.00','$1.80','50.00%']]
    billing=Page((out/'guides/per-gpu-hour-vs-instance/index.html').read_text(encoding='utf-8'))
    assert any('USD $1.60/hr' in c['roles']['minimum-instance'] for c in billing.cards)
    assert any('USD $0.027 / 60' in c['roles']['minimum-instance'] for c in billing.cards)
    assert any('USD $16.00/hr' in c['roles']['minimum-instance'] for c in billing.cards)
    value=Page((out/'guides/cheapest-gpu-per-vram-gb/index.html').read_text(encoding='utf-8'))
    t4=next(r for r in value.table_rows if r[:2]==['T4 16GB','On-demand'])
    assert t4[2:4]==['16','0.012500']
    assert value.table_rows[0][0]=='H100 SXM 80GB'
    assert not any(r[0] in {'No capacity','Unknown GPU 20GiB'} for r in value.table_rows)
    assert 'excluded-' not in value.text
    assert result['audit']['guide_pages']==48


def test_all_languages_breadcrumbs_articles_links_and_sitemaps(guide_site):
    _,out,_=guide_site;catalogs=load_catalogs()
    sm=ET.parse(out/'sitemap.xml')
    indexed={n.text for n in sm.findall('.//{*}loc')}
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        for slug,(key,_) in GUIDES.items():
            path=out/lang.prefix/'guides'/slug/'index.html'
            raw=path.read_text(encoding='utf-8');page=Page(raw)
            assert page.h1==[tr.text('guide_'+key+'_title')]
            assert [x['@type'] for x in page.structured]==['Article','BreadcrumbList']
            assert page.structured[0]['dateModified']=='2026-11-13T08:00:00.000000Z'
            assert len(page.structured[1]['itemListElement'])==3
            assert ' / '+tr.text('guide_index_title')+' / ' in page.breadcrumbs[0]
            assert not re.search(r'\bAI\b',page.text)
            assert ORIGIN+lang.path('/guides/'+slug+'/') in indexed
            assert len([a for tag,a in page.links if tag=='link' and a.get('hreflang')])==9
        root=Page((out/lang.prefix/'index.html').read_text(encoding='utf-8'))
        assert any(a.get('href')==lang.path('/guides/') for tag,a in root.links)
        assert (out/('sitemap-'+lang.code+'.xml')).is_file()
    assert len({catalogs[l.code].text('guide_memory') for l in LANGUAGES})==8


def test_no_pairing_across_provider_region_or_allocation():
    rows=population()
    data={}
    for row in rows:
        if row.day==DAY and row.current_at(STAMP):data.setdefault(row.gpu_model,{'offers':[]})['offers'].append(row.to_dict())
    stats=guides.snapshot(data,REFERENCE_MODELS)
    assert [(d['model'],d['count']) for d in stats['discounts']]==[('H100 SXM 80GB',3)]
    # Opposite provider, identical other dimensions cannot manufacture a pair.
    stats=guides.snapshot({'H100 SXM 80GB':{'offers':[
        offer(rate=2).to_dict(),offer(rate=1,provider='linode',price_type='spot').to_dict()]}},REFERENCE_MODELS)
    assert not stats['discounts']


def test_thin_content_and_legacy_reference_table_fail_closed(tmp_path):
    with PriceStore(':memory:') as store:
        store.upsert([offer()])
        store.connection.execute('DROP TABLE guide_reference_models')
        out=tmp_path/'sparse'
        result=build_site(store,out,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
        assert not any('/guides/' in u for u in result['sitemap_urls'])
        for slug in ('',*GUIDES):
            page=Page((out/'guides'/slug/'index.html').read_text(encoding='utf-8'))
            assert page.metadata['robots']=='noindex'
        choose=Page((out/'guides/choose-gpu-cloud-llm-inference/index.html').read_text(encoding='utf-8'))
        assert not any(row[0].startswith('Llama') for row in choose.table_rows)
        spot=Page((out/'guides/spot-vs-on-demand/index.html').read_text(encoding='utf-8'))
        assert not spot.table_rows and 'guide:missing_pairs' in spot.search_values


def test_negative_discounts_are_not_clamped():
    stats=guides.snapshot({'H100 SXM 80GB':{'offers':[
        offer(rate=1).to_dict(),offer(rate=2,price_type='spot').to_dict()]}},REFERENCE_MODELS)
    assert stats['discounts'][0]['percent']==-100


@pytest.mark.parametrize('demand,spot,display',[(3,2.99985,'0.00'),(10,9.9985,'0.02'),(80,79.988,'0.02')])
def test_discount_decimal_boundaries_agree_with_independent_rational_audit(demand,spot,display):
    from pipelines.gpu_prices.audit import _snapshot
    from pipelines.gpu_prices.guide_audit import snapshot as audit_snapshot
    rows=[offer(rate=demand),offer(rate=spot,price_type='spot')]
    rendered=guides.snapshot({'H100 SXM 80GB':{'offers':[r.to_dict() for r in rows]}},REFERENCE_MODELS)
    with PriceStore(':memory:') as store:
        store.upsert(rows)
        errors=[];audited=audit_snapshot(_snapshot(store.connection,DAY,STAMP),errors)
    assert not errors
    assert format(rendered['discounts'][0]['percent'],'.2f')==display
    assert format(audited['discounts'][0]['percent'],'.2f')==display


def test_audit_does_not_import_renderer_statistics(guide_site,monkeypatch):
    store,out,_=guide_site
    def forbidden(*args,**kwargs):raise AssertionError('renderer is not an oracle')
    monkeypatch.setattr(guides,'snapshot',forbidden)
    monkeypatch.setattr(guides,'render',forbidden)
    assert audit_site(store.connection,out)['status']=='passed'


@pytest.mark.parametrize('damage',[
    'discount','weight','ratio','missing-marker','hidden-article','hidden-value','source',
    'source-label','internal-link','article','breadcrumb','explanation','qualifier','unmarked-number',
    'forbidden-word','table-order','missing-row','tax-label','minimum-charge',
    'units','caption','css-hidden','source-hidden',
])
def test_strict_audit_rejects_changed_guides(guide_site,tmp_path,damage):
    store,source,_=guide_site
    out=tmp_path/'site';shutil.copytree(source,out)
    slug='cheapest-gpu-per-vram-gb'
    if damage=='discount':slug='spot-vs-on-demand'
    if damage in {'weight','source','source-label','explanation'}:slug='choose-gpu-cloud-llm-inference'
    path=out/'guides'/slug/'index.html';raw=path.read_text(encoding='utf-8')
    if damage=='discount':raw=raw.replace('>50.00%</span>','>99.00%</span>',1)
    elif damage=='weight':raw=raw.replace('/weights/0/fp16" data-format="number">14','/weights/0/fp16" data-format="number">15',1)
    elif damage=='ratio':raw=raw.replace('data-format="precise">0.012500','data-format="precise">0.000001',1)
    elif damage=='missing-marker':raw=raw.replace('data-stat="/guides/values/0/ratio"','data-fake="/guides/values/0/ratio"',1)
    elif damage=='hidden-article':raw=raw.replace('<article data-guide=','<article hidden data-guide=',1)
    elif damage=='hidden-value':raw=raw.replace('data-format="precise"','data-format="precise" aria-hidden="true"',1)
    elif damage=='source':raw=raw.replace('href="https://huggingface.co/docs/accelerate/usage_guides/model_size_estimator"','href="https://example.test/unofficial"',1)
    elif damage=='source-label':raw=raw.replace('Meta: Llama model card','Unofficial sizing claim',1)
    elif damage=='internal-link':raw=raw.replace('href="/compare/','href="/missing/',1)
    elif damage=='article':raw=raw.replace('"@type": "Article"','"@type": "FAQPage"',1)
    elif damage=='breadcrumb':raw=raw.replace('"position": 3','"position": 9',1)
    elif damage=='explanation':raw=raw.replace('data-search="guide:memory"','data-search="guide:invented"',1)
    elif damage=='qualifier':raw=raw.replace('data-search="guide:basis"','data-search="guide:wrong-basis"',1)
    elif damage=='unmarked-number':raw=raw.replace('</article>','<p>Only $0.001 everywhere</p></article>',1)
    elif damage=='forbidden-word':raw=raw.replace('</article>','<p>AI pricing</p></article>',1)
    elif damage=='table-order':
        rows=re.findall(r'<tr><td>.*?</tr>',raw)
        raw=raw.replace(rows[0],'SWAP',1).replace(rows[1],rows[0],1).replace('SWAP',rows[1],1)
    elif damage=='missing-row':raw=re.sub(r'<tr><td>.*?</tr>','',raw,count=1)
    elif damage=='tax-label':raw=raw.replace('Tax unknown','Tax exempt',1)
    elif damage=='minimum-charge':raw=raw.replace('data-format="minimum-charge-money">','data-format="minimum-charge-money">9',1)
    elif damage=='units':raw=raw.replace('USD/GB VRAM/hour','USD/hour',1)
    elif damage=='caption':raw=raw.replace('<caption>','<caption>Wrong basis: ',1)
    elif damage=='css-hidden':raw=raw.replace('</style>','article{display:none}</style>',1)
    elif damage=='source-hidden':raw=raw.replace('href="https://huggingface.co/docs/accelerate/usage_guides/model_size_estimator"','hidden href="https://huggingface.co/docs/accelerate/usage_guides/model_size_estimator"',1)
    assert raw!=path.read_text(encoding='utf-8'),damage
    path.write_text(raw,encoding='utf-8')
    # Refresh unrelated deployment hashes: corruption must fail semantic checks.
    manifest_path=out/'indexnow-manifest.json';manifest=json.loads(manifest_path.read_text())
    url=ORIGIN+'/guides/'+slug+'/'
    normalized=re.sub(r'<time datetime="[^"]*" data-stat="/built" data-format="date">[^<]*</time>','<time data-stat="/built"></time>',raw)
    manifest['pages'][url]=hashlib.sha256(normalized.encode()).hexdigest()
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(AuditError):audit_site(store.connection,out)


def test_audit_rejects_forged_model_reference(guide_site):
    store,out,_=guide_site
    store.connection.execute("UPDATE guide_reference_models SET parameters_billions=8 WHERE model='Llama 2 7B'")
    try:
        with pytest.raises(ValueError,match='guide reference database'):reference_rows(store.connection)
        with pytest.raises(AuditError,match='guide reference database'):audit_site(store.connection,out)
    finally:
        store.connection.execute("UPDATE guide_reference_models SET parameters_billions=7 WHERE model='Llama 2 7B'")


def test_guides_namespace_is_reserved():
    assert model_slugs(['Guides'])['Guides']!='guides'
