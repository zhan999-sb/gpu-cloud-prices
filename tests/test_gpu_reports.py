"""Weekly evidence, archive semantics, localization and adversarial audit checks."""
from dataclasses import replace
import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET

import pytest

from pipelines.gpu_prices import reports
from pipelines.gpu_prices.audit import AuditError, Page, _snapshot, audit_site
from pipelines.gpu_prices.build_site import build_site, model_slugs
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.report_audit import snapshot as audit_snapshot
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore

DAY='2026-11-13'
STAMP=DAY+'T12:00:00Z'
ORIGIN='https://gpu.example.test'


def offer(model='H100 SXM 80GB',rate=2,day=DAY,sku='h100',**changes):
    args=dict(provider='azure',sku=sku,gpu_model=model,gpu_count=1,vcpu=8,ram_gb=32,
        region='eastus',price_type='on_demand',currency='USD',price_per_hour=rate,
        price_per_gpu_hour=rate,source_url='https://example.test/prices',
        collected_at=day+'T08:00:00Z',tax_basis='unknown',minimum_billing_seconds=60)
    args.update(changes)
    return Offer(**args)


def population():
    rows=[]
    for day in ['2026-10-30','2026-11-06',DAY]:
        scale=1 if day==DAY else 2
        rows += [offer(rate=2*scale,day=day),
                 offer('A100 80GB',3*scale,day,'a100'),
                 offer('L40S 48GB',1*scale,day,'l40s'),
                 offer(rate=3 if day==DAY else 1,day=day,price_type='spot')]
    rows += [offer('RTX 4090 24GB',.2,DAY,'new',provider='linode'),
             offer('RTX 5090 32GB',1,'2026-11-06','old',provider='verda')]
    return rows


@pytest.fixture(scope='module')
def weekly_site(tmp_path_factory):
    folder=tmp_path_factory.mktemp('reports')
    store=PriceStore(':memory:');store.upsert(population())
    result=build_site(store,folder,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    yield store,folder,result
    store.close()


def data_for(rows,day=DAY,built=STAMP):
    with PriceStore(':memory:') as store:
        store.upsert(rows)
        result=reports.snapshot(store.observation_rows(),[o.to_dict() for o in store.current_prices(day,built) if o.collected_at<=built],day,built)
        independent=audit_snapshot(_snapshot(store.connection,day,built))
    # SQL audit uses its own normalization; visible facts must agree.
    for key,d in result.items():
        assert [(p['now']['gpu_model'],p['now']['price_type'],p['change']) for p in d['comparisons']]==[(p['now']['gpu_model'],p['now']['price_type'],p['change']) for p in independent[key]['comparisons']]
        assert d['offers']==independent[key]['offers']
    return result


def test_weekly_prices_counts_moves_and_listing_changes(weekly_site):
    store,out,result=weekly_site
    data=data_for(population())['2026-W46']
    assert data['offers']==5 and data['providers']==2
    assert data['before_offers']==5 and data['before_providers']==2
    assert len(data['comparisons'])==4 and data['indexable']
    assert data['moves'][0]['now']['price_type']=='spot'
    assert data['moves'][0]['change']==200
    assert data['new_models']==['RTX 4090 24GB']
    assert data['missing_models']==['RTX 5090 32GB']
    assert data['new_providers']==['linode'] and data['missing_providers']==['verda']
    assert result['audit']['report_pages']==32
    assert result['audit']['indexable_reports']==16
    page=Page((out/'reports/2026-W46/index.html').read_text(encoding='utf-8'))
    assert ['H100 SXM 80GB','On-demand','Azure','eastus','$2.00','$4.00','-50.00%'] in [r[:7] for r in page.table_rows]
    assert page.table_rows[0]==['Snapshot','5','2']
    assert audit_site(store.connection,out)['status']=='passed'


def test_all_locales_articles_breadcrumbs_links_sitemaps_and_feed(weekly_site):
    _,out,_=weekly_site
    catalogs=load_catalogs()
    indexed={n.text for n in ET.parse(out/'sitemap.xml').findall('.//{*}loc')}
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        path=out/lang.prefix/'reports/2026-W46/index.html'
        raw=path.read_text(encoding='utf-8');page=Page(raw)
        assert page.h1==[tr.text('report_title',week='2026-W46')]
        assert [d['@type'] for d in page.structured]==['Article','BreadcrumbList']
        assert 'datePublished' not in page.structured[0]
        assert page.structured[0]['dateModified']==DAY+'T08:00:00.000000Z'
        assert any(p=='/reports/2026-W46/published' and text==DAY+'T08:00:00.000000Z' for p,fmt,text in page.values)
        assert len(page.structured[1]['itemListElement'])==3
        assert len([a for tag,a in page.links if tag=='link' and a.get('hreflang')])==9
        assert not re.search(r'\bAI\b',page.text)
        assert ORIGIN+lang.path('/reports/2026-W46/') in indexed
        assert ORIGIN+lang.path('/reports/') in indexed
        assert ORIGIN+lang.path('/reports/2026-W44/') not in indexed
        assert {lang.path('/h100-sxm-80gb/'),lang.path('/providers/azure/'),lang.path('/guides/spot-vs-on-demand/')} <= {a.get('href') for tag,a in page.links}
        home=Page((out/lang.prefix/'index.html').read_text(encoding='utf-8'))
        assert any(a.get('data-report-latest')=='true' and a['href']==lang.path('/reports/2026-W46/') for tag,a in home.links)
        local={n.text for n in ET.parse(out/('sitemap-'+lang.code+'.xml')).findall('.//{*}loc')}
        assert ORIGIN+lang.path('/reports/2026-W46/') in local
        items=ET.parse(out/lang.prefix/'feed.xml').findall('./channel/item')
        links=[n.findtext('link') for n in items]
        assert links.count(ORIGIN+lang.path('/reports/2026-W46/'))==1
        assert ORIGIN+lang.path('/reports/2026-W45/') in links
        assert ORIGIN+lang.path('/reports/2026-W44/') in links
        thin=next(n for n in items if n.findtext('link')==ORIGIN+lang.path('/reports/2026-W44/'))
        assert tr.text('report_short') in thin.findtext('description')


def test_sparse_history_and_initial_import_never_fabricate_changes(tmp_path):
    with PriceStore(':memory:') as store:
        store.upsert([offer(),offer('A100 80GB',3,sku='a'),offer('L40S 48GB',1,sku='l')])
        result=build_site(store,tmp_path,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
        assert not any('/reports/' in u for u in result['sitemap_urls'])
        page=Page((tmp_path/'reports/2026-W46/index.html').read_text(encoding='utf-8'))
        assert page.metadata['robots']=='noindex'
        assert 'report:short' in page.search_values
        assert 'report:no_comparisons' in page.search_values
        assert page.search_values['report:new_models']=='Not observed'
        items=ET.parse(tmp_path/'feed.xml').findall('./channel/item')
        weekly=[n for n in items if '/reports/' in n.findtext('link')]
        assert len(weekly)==1 and load_catalogs()['en'].text('report_short') in weekly[0].findtext('description')


@pytest.mark.parametrize('changes',[
    {'provider':'linode'},{'gpu_model':'H100 PCIe 80GB'}, {'region':'westus'},
    {'gpu_count':2,'price_per_hour':2,'price_per_gpu_hour':1}, {'price_type':'spot'}, {'tax_basis':'excluded'},
    {'price_scope':'per_gpu'}, {'minimum_billing_seconds':3600},
    {'original_currency':'EUR','original_price':2,'fx_rate':1,'fx_date':DAY,
     'fx_source':'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'}, {'price_conditions':'conditional offer'},
    {'billing_unit':'instance-minute'}, {'source_billing_unit':'GPU-hour'},
    {'price_basis':'another basis'},
])
def test_no_pairing_across_comparison_dimensions(changes):
    rows=[offer(day='2026-11-06'),replace(offer(),**changes)]
    assert not data_for(rows)['2026-W46']['comparisons']


@pytest.mark.parametrize('changes',[
    {'is_from_price':True},{'tax_basis':'included'},{'price_scope':'gpu_component'},
    {'gpu_count':.5,'gpu_fraction':.5,'price_per_gpu_hour':4},
    {'region':'chinaeast'},{'region':'attatlanta1'}, {'currency':'EUR'},
    {'effective_from':'2026-11-14T00:00:00Z'}, {'collected_at':'2026-11-13T14:00:00Z'},
])
def test_ineligible_and_future_rows_do_not_create_prices_or_pairs(changes):
    rows=[offer(day='2026-11-06'),replace(offer(),**changes)]
    d=data_for(rows)['2026-W46']
    assert not d['comparisons'] and not d['minima'] and d['offers']==0


def test_never_uses_nearest_day_or_fills_a_gap():
    d=data_for([offer(day='2026-11-05'),offer()])['2026-W46']
    assert not d['baseline'] and not d['comparisons']


def test_common_group_can_differ_from_unrestricted_cheapest():
    d=data_for([offer(day='2026-11-06',rate=4),offer(rate=3),offer(rate=.1,provider='linode',sku='new-cheap')])['2026-W46']
    assert d['minima'][0]['price_per_gpu_hour']==.1
    assert d['comparisons'][0]['now']['price_per_gpu_hour']==3
    assert d['comparisons'][0]['before']['price_per_gpu_hour']==4


def test_archive_endpoints_iso_year_boundary_and_observation_cutoff():
    rows=[offer(day='2026-12-25'),offer(day='2027-01-01'),offer(day='2027-01-04'),offer(day='2027-01-05')]
    d=data_for(rows,'2027-01-04','2027-01-04T12:00:00Z')
    assert list(d)==['2027-W01','2026-W53','2026-W52']
    assert d['2026-W53']['day']=='2027-01-01'
    assert d['2026-W53']['before_day']=='2026-12-25'
    assert d['2027-W01']['updated'].startswith('2027-01-04')


def test_current_failures_exclude_stale_provider_and_never_announce_initial_import():
    with PriceStore(':memory:') as store:
        store.upsert([offer(day='2026-11-06'),offer()])
        store.record_run(provider='azure',started_at=DAY+'T07:00:00Z',status='failed',pages=0,records=0)
        d=reports.snapshot(store.observation_rows(),[o.to_dict() for o in store.current_prices(DAY,STAMP)],DAY,STAMP)['2026-W46']
        assert not d['minima'] and not d['comparisons'] and not d['missing_providers']


def test_empty_report_exists_and_reserved_route_cannot_be_stolen(tmp_path):
    assert model_slugs(['reports'])['reports']!='reports'
    with PriceStore(':memory:') as store:
        result=build_site(store,tmp_path,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    assert result['audit']['report_pages']==16
    page=Page((tmp_path/'reports/2026-W46/index.html').read_text(encoding='utf-8'))
    assert page.metadata['robots']=='noindex'
    assert 'datePublished' not in page.structured[0]


def test_audit_independent_of_feature_code(weekly_site,monkeypatch):
    store,out,_=weekly_site
    def refuse(*a,**kw):raise AssertionError('renderer cannot supply audit expectations')
    for name in ['snapshot','render','article','feed_events']:monkeypatch.setattr(reports,name,refuse)
    assert audit_site(store.connection,out)['status']=='passed'


@pytest.mark.parametrize('damage',[
    'price','percent','count','missing-row','listing','honesty','hidden','stylesheet',
    'article','breadcrumb','canonical','noindex','sitemap','feed','latest','units','new-number',
    'row-link','source-link','tax-label',
])
def test_audit_rejects_fabricated_or_hidden_report_content(weekly_site,tmp_path,damage):
    store,source,_=weekly_site
    out=tmp_path/'site';shutil.copytree(source,out)
    path=out/'reports/2026-W46/index.html'
    raw=path.read_text(encoding='utf-8')
    if damage=='price':raw=raw.replace('$2.00','$0.01',1)
    elif damage=='percent':raw=raw.replace('200.00%','900.00%',1)
    elif damage=='count':raw=raw.replace('data-stat="/reports/2026-W46/offers" data-format="number">5','data-stat="/reports/2026-W46/offers" data-format="number">999',1)
    elif damage=='missing-row':raw=re.sub(r'<tr><td><a[^>]*>H100.*?</tr>','',raw,count=1)
    elif damage=='listing':raw=raw.replace('>RTX 4090 24GB</a></li>','>RTX 5090 32GB</a></li>',1)
    elif damage=='honesty':raw=raw.replace('No dates or missing prices are filled in.','All missing prices are filled in.',1)
    elif damage=='hidden':raw=raw.replace('<article data-report=','<article hidden data-report=',1)
    elif damage=='stylesheet':raw=raw.replace('</style>','[data-report]{display:none}</style>',1)
    elif damage=='article':raw=raw.replace('"headline": "GPU cloud price report: 2026-W46"','"headline": "Fabricated"',1)
    elif damage=='breadcrumb':raw=raw.replace('"position": 3','"position": 8',1)
    elif damage=='canonical':raw=raw.replace('rel="canonical" href="'+ORIGIN,'rel="canonical" href="https://wrong.test',1)
    elif damage=='noindex':raw=raw.replace('</head>','<meta name="robots" content="noindex"/></head>',1)
    elif damage=='sitemap':
        path=out/'sitemap.xml';raw=path.read_text(encoding='utf-8').replace('/reports/2026-W46/','/reports/2099-W46/')
    elif damage=='feed':
        path=out/'feed.xml';raw=path.read_text(encoding='utf-8').replace('/reports/2026-W46/','/reports/2099-W46/')
    elif damage=='latest':
        path=out/'index.html';raw=path.read_text(encoding='utf-8').replace('data-report-latest="true" href="/reports/2026-W46/"','data-report-latest="true" href="/reports/2026-W45/"',1)
    elif damage=='units':raw=raw.replace('USD / GPU-hour; price types are separate','USD / instance-hour',1)
    elif damage=='new-number':raw=raw.replace('</article>','<p>Saved $999 this week</p></article>',1)
    elif damage=='row-link':raw=raw.replace('<td><a href="/h100-sxm-80gb/">','<td><a href="/a100-80gb/">',1)
    elif damage=='source-link':raw=raw.replace('href="https://example.test/prices"','href="https://wrong.test/prices"',1)
    elif damage=='tax-label':raw=raw.replace('Tax unknown','Tax excluded',1)
    assert raw!=path.read_text(encoding='utf-8')
    path.write_text(raw,encoding='utf-8')
    # Keep the ordinary content hash valid: rejection must come from the SQL
    # evidence/content gate, rather than merely noticing a changed HTML file.
    if path.suffix=='.html':
        manifest_path=out/'indexnow-manifest.json'
        manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
        relative=path.relative_to(out).as_posix()
        route='/' if relative=='index.html' else '/'+relative.removesuffix('index.html')
        url=ORIGIN+route
        if url in manifest['pages']:
            normalized=re.sub(r'<time datetime="[^"]*" data-stat="/built" data-format="date">[^<]*</time>','<time data-stat="/built"></time>',raw)
            manifest['pages'][url]=hashlib.sha256(normalized.encode('utf-8')).hexdigest()
            manifest_path.write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    with pytest.raises(AuditError):audit_site(store.connection,out)


def test_report_guid_is_week_stable_and_archive_unchanged_on_later_rebuild(weekly_site,tmp_path):
    store,source,_=weekly_site
    original=(source/'reports/2026-W45/index.html').read_text(encoding='utf-8')
    build_site(store,tmp_path,today=DAY,built_at=DAY+'T15:00:00Z',site_base_url=ORIGIN)
    normalized=lambda raw:re.sub(r'<time datetime="[^"]*" data-stat="/built" data-format="date">[^<]*</time>','BUILD',raw)
    assert normalized(original)==normalized((tmp_path/'reports/2026-W45/index.html').read_text(encoding='utf-8'))
    first=reports.feed_events(data_for(population()),DAY)
    from pipelines.gpu_prices.audience import feed
    catalogs=load_catalogs();slugs=model_slugs(store.models())
    a=ET.fromstring(feed(first,slugs,ORIGIN,DAY,catalogs['en']))
    changed=[{**e,'day':'2026-11-14'} if e['week']=='2026-W46' else e for e in first]
    b=ET.fromstring(feed(changed,slugs,ORIGIN,'2026-11-14',catalogs['en']))
    assert a.findtext('./channel/item/guid')==b.findtext('./channel/item/guid')
