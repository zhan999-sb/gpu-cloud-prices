"""b18 growth features: evidence boundaries and independent corruption gates."""
from datetime import date, timedelta
from html import escape
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from pipelines.gpu_prices import audience
from pipelines.gpu_prices.audit import AuditError, Page, audit_site
from pipelines.gpu_prices.build_site import build_site, model_slugs
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore

DAY='2026-11-12'
STAMP=DAY+'T12:00:00Z'
ORIGIN='https://gpu.example.test'


def quote(model='H100 SXM 80GB', price=10, day=DAY, **changes):
    fields=dict(provider='azure',sku=model,gpu_model=model,gpu_count=1,vcpu=8,ram_gb=32,
        region=None,price_type='on_demand',currency='USD',price_per_hour=price,
        price_per_gpu_hour=price,source_url='https://example.test/prices',collected_at=day+'T12:00:00Z')
    fields.update(changes)
    return Offer(**fields)


@pytest.fixture(scope='module')
def growth_site(tmp_path_factory):
    folder=tmp_path_factory.mktemp('audience');store=PriceStore(':memory:')
    rows=[]
    for n in range(8):
        day=(date.fromisoformat(DAY)-timedelta(days=7-n)).isoformat()
        rows.extend([quote(price=8.5 if n==7 else 10,day=day),
            quote('Rig GPU',9 if n==7 else 10,day=day),
            quote('B-only GPU',.1,day=day,provider='nebius',tier='B',export_allowed=False)])
        if n>=6:rows.append(quote('New GPU',20,day=day,provider='linode'))
    rows.extend([quote('Future GPU',.01,day='2026-11-20',provider='verda'),
        quote('H100 SXM 80GB',.01,provider='paperspace',tier='C',export_allowed=False,is_from_price=True),
        quote('Stale GPU',1,day='2026-11-11')])
    store.upsert(rows)
    result=build_site(store,folder,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    yield store,folder,result
    store.close()


def test_embeds_data_feeds_badges_in_eight_languages(growth_site):
    store,folder,result=growth_site;catalogs=load_catalogs()
    assert result['audit']['status']=='passed'
    assert result['audit']['embed_pages']==5*8
    assert result['audit']['max_embed_bytes']<15_000
    assert result['audit']['feed_items']==4  # two daily events and two honest weekly reports
    assert result['audit']['price_drop_badges']==2*8
    assert result['html_pages']==result['audit']['indexed_pages']+result['audit']['noindex_pages']
    assert sum(result['html_pages_by_language'].values())==result['html_pages']
    index=json.loads((folder/'data/latest.json').read_text(encoding='utf-8'))
    assert {r['gpu_model'] for r in index['exports']}=={'H100 SXM 80GB','Rig GPU','New GPU','Stale GPU'}
    assert index['license']==audience.LICENSE
    for item in index['exports']:
        data=json.loads((folder/item['url'].removeprefix(ORIGIN+'/')).read_text(encoding='utf-8'))
        assert all(r['tier']=='A' and r['export_allowed'] for r in data['offers'])
        assert item['last_updated']==data['last_updated'] and item['generated_at']==data['generated_at']
    headers=(folder/'_headers').read_text(encoding='utf-8')
    assert [line for line in headers.splitlines() if not line.startswith(' ')]==['/embed/*']
    assert "frame-ancestors *" in headers and "'unsafe-inline'" not in headers
    assert '! X-Frame-Options' in headers
    for lang in LANGUAGES:
        tr=catalogs[lang.code]
        widget=folder/audience.embed_path('h100-sxm-80gb',lang).lstrip('/')
        page=Page(widget.read_text(encoding='utf-8'))
        assert '$8.50' in page.text and tr.text('tax_unknown') in page.text and tr.text('unknown_region') in page.text
        assert page.metadata['robots']=='noindex' and not any(t=='script' for t,_ in page.links)
        empty=Page((folder/audience.embed_path('b-only-gpu',lang).lstrip('/')).read_text(encoding='utf-8'))
        assert tr.text('growth_embed_empty') in empty.text and '$0.100' not in empty.text
        stale=Page((folder/audience.embed_path('stale-gpu',lang).lstrip('/')).read_text(encoding='utf-8'))
        assert tr.text('growth_embed_empty') in stale.text
        model=Page((folder/lang.prefix/'h100-sxm-80gb/index.html').read_text(encoding='utf-8'))
        code=next(n for n in model.growth_nodes if n[0]=='textarea')
        assert ORIGIN+audience.embed_path('h100-sxm-80gb',lang) in code[2] and 'readonly' in code[1]
        root=ET.parse(folder/lang.prefix/'feed.xml').getroot()
        items=root.findall('channel/item')
        weekly=[n for n in items if '/reports/' in n.findtext('link')]
        assert len(items)==4 and len(weekly)==2
        assert all(tr.text('report_thin') in n.findtext('description') for n in weekly)
        assert 'B-only' not in ET.tostring(root,encoding='unicode')
        assert root.findtext('channel/language')==lang.code
        sitemap=(folder/('sitemap-'+lang.code+'.xml')).read_text(encoding='utf-8')
        assert '/embed/' not in sitemap and ORIGIN+lang.path('/data/') in sitemap


@pytest.mark.parametrize('price,missing,kind,expected',[(9,False,'on_demand',True),(9.0001,False,'on_demand',False),
    (1,True,'on_demand',False),(1,False,'spot',False)])
def test_badge_boundary_complete_seven_days_same_type(price,missing,kind,expected):
    points=[dict(day=(date.fromisoformat(DAY)-timedelta(days=n)).isoformat(),min=10) for n in range(1,8)]
    if missing:points.pop()
    points.append(dict(day=DAY,min=price))
    row=quote(price=price,price_type=kind).to_dict()
    assert bool(audience.price_drop({'history':{'on_demand':points}},row,DAY))==expected


@pytest.mark.parametrize('before,now,offset,popular,reason',[(10,8.5,1,False,'drop'),(10,8.5001,1,False,None),
    (10,9,1,True,'low'),(10,9,1,False,None),(10,1,2,True,None),(10,10,1,True,None)])
def test_feed_thresholds_and_no_missing_day_estimates(before,now,offset,popular,reason):
    model='H100 SXM 80GB' if popular else 'Rig GPU'
    old=(date.fromisoformat(DAY)-timedelta(days=offset)).isoformat()
    data={model:{'history':{'on_demand':[dict(day=old,min=before),dict(day=DAY,min=now)]},'offers':[]}}
    items=audience.events(data,[],DAY,STAMP)
    assert [i['reason'] for i in items]==([reason] if reason else [])


def test_feed_cap_order_determinism_and_first_snapshot():
    model='H100 SXM 80GB';points=[]
    for n in range(40):points.append(dict(day=(date.fromisoformat(DAY)-timedelta(days=39-n)).isoformat(),min=100*.8**n))
    data={model:{'history':{'on_demand':points,'spot':points},'offers':[]}}
    items=audience.events(data,[],DAY,STAMP)
    assert len(items)==50 and items==audience.events(data,[],DAY,STAMP)
    assert all(i['day']>='2026-10-14' for i in items)
    raw=audience.feed(items,{model:'h100-sxm-80gb'},ORIGIN,DAY,load_catalogs()['en'])
    assert len({i.findtext('guid') for i in ET.fromstring(raw).findall('channel/item')})==50
    rows=[{**quote(provider=p).to_dict(),'day':DAY} for p in ('azure','linode')]
    assert audience.events({},rows,DAY,STAMP)==[]


def test_route_collisions_reserved_growth_paths():
    slugs=model_slugs(['Data','Embed','Feed','Latest','H100 SXM 80GB'])
    assert not {'data','embed','feed','latest'}&set(slugs.values())


@pytest.mark.parametrize('filename,old,new',[
    ('embed/h100-sxm-80gb.html','$8.50','$0.01'),
    ('embed/h100-sxm-80gb.html','Tax unknown','Tax included'),
    ('embed/h100-sxm-80gb.html','rel="nofollow noopener"','rel="sponsored"'),
    ('embed/h100-sxm-80gb.html','content="noindex"','content="index"'),
    ('embed/h100-sxm-80gb.html','</body>','<script src="https://bad.test/x.js"></script></body>'),
    ('embed/h100-sxm-80gb.html','</head>','<meta http-equiv="refresh" content="0;url=https://bad.test"/></head>'),
    ('embed/h100-sxm-80gb.html','font-size:28px','display:none;font-size:28px'),
    ('_headers','frame-ancestors *',"frame-ancestors 'none'"),
    ('_headers','/embed/*','/*'),
    ('data/latest.json','H100 SXM 80GB','B-only GPU'),
    ('data/index.html','CC BY 4.0','All rights granted'),
    ('data/index.html',' · 2026-11-12T12:00:00.000000Z',' · 2026-11-13T12:00:00Z'),
    ('feed.xml','$8.50','$0.01'),
    ('feed.xml','isPermaLink="false"','isPermaLink="true"'),
    ('index.html','data-growth="drop"','data-growth="unexpected"'),
    ('index.html','Price drop · at least 10%','Price drop · at least 50%'),
    ('h100-sxm-80gb/index.html','width=&quot;360&quot;','width=&quot;999&quot;'),
    ('h100-sxm-80gb/index.html','<section data-growth="embed">','<section hidden data-growth="embed">'),
])
def test_independent_audit_rejects_growth_corruption(growth_site,filename,old,new):
    store,folder,_=growth_site;path=folder/filename
    original=path.read_text(encoding='utf-8');assert old in original
    try:
        path.write_text(original.replace(old,new,1),encoding='utf-8')
        with pytest.raises(AuditError):audit_site(store.connection,folder)
    finally:path.write_text(original,encoding='utf-8')


def test_stale_cleanup_and_unrelated_file_preservation(growth_site,tmp_path):
    store,folder,_=growth_site
    small=PriceStore(':memory:');small.upsert([quote('Tiny GPU',1)])
    (tmp_path/'unrelated.txt').write_text('keep',encoding='utf-8')
    build_site(store,tmp_path,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    build_site(small,tmp_path,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    assert not (tmp_path/'embed/h100-sxm-80gb.html').exists()
    assert (tmp_path/'unrelated.txt').read_text(encoding='utf-8')=='keep'
    index=json.loads((tmp_path/'data/latest.json').read_text(encoding='utf-8'))
    assert [r['gpu_model'] for r in index['exports']]==['Tiny GPU']
    small.close()
