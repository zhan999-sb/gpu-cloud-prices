"""Real trimmed official catalogs, offline injections, arithmetic and budgets."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

from functools import partial

import pytest

from pipelines.gpu_prices import http
from pipelines.gpu_prices.audit import audit_site
from pipelines.gpu_prices.build_site import build_site
from pipelines.gpu_prices.fx import to_usd, URL as FX_URL
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.providers import hyperscalers as h
from pipelines.gpu_prices.providers.registry import PROVIDERS
from pipelines.gpu_prices.regions import eligible, region_class
from pipelines.gpu_prices.schema import parse_utc
from pipelines.gpu_prices.store import PriceStore

# This module tests its feature in all eight languages. The complete workload
# matrix and its independent audit remain in test_gpu_workloads.py.
build_site = partial(build_site, include_workloads=False)
audit_site = partial(audit_site, require_workloads=False)

ROOT = Path(__file__).parent / 'fixtures/hyperscalers'
STAMP = '2026-11-13T00:00:00Z'
CLASSES = [h.AwsCollector, h.OracleCollector, h.OvhCollector, h.VultrCollector, h.HetznerCollector]


def blob(name):
    return (ROOT/name).read_text(encoding='utf-8')


def fetch(url):
    name = ('aws_index.json' if url == h.AWS_INDEX else 'aws_spot.json' if url == h.AWS_SPOT else
            'oracle.json' if url == h.ORACLE else 'ovh.json' if url == h.OVH else
            'hetzner.html' if url == h.HETZNER else 'vultr_metal.json' if 'plans-metal' in url else 'vultr.json')
    return blob(name) if name.endswith('.html') else json.loads(blob(name))


def collector(cls, store, **kw):
    if cls == h.AwsCollector:
        kw.setdefault('regions', ['us-east-1'])
        kw.setdefault('stream', lambda u: iter(blob('aws.csv').splitlines(keepends=True)))
    return cls(store=store, fetch=kw.pop('fetch', fetch), clock=kw.pop('clock', lambda: STAMP), **kw)


def rows(cls):
    with PriceStore(':memory:') as s:
        return collector(cls, s).collect()


def test_real_trimmed_fixture_receipts():
    manifest = json.loads(blob('manifest.json'))
    assert manifest['request'] == {'method':'GET','auth':False,'cookies':False}
    for name, meta in manifest['fixtures'].items():
        b = (ROOT/name).read_bytes()
        assert len(b) == meta['fixture_bytes'] < 30_000
        assert hashlib.sha256(b).hexdigest() == meta['fixture_sha256']
        assert meta['source_bytes'] > 0  # JSON pretty printing may increase byte length.
        assert meta['source_url'].startswith('https://') and meta['trim']


@pytest.mark.parametrize('cls', CLASSES)
def test_catalog_math_policy_and_idempotent_storage(cls):
    with PriceStore(':memory:') as store:
        c = collector(cls, store)
        offers = c.collect()
        assert offers and c.complete and not c.skipped
        assert all(o.price_scope == 'instance' and o.price_per_gpu_hour == pytest.approx(o.price_per_hour/o.gpu_count) for o in offers)
        assert all(o.gpu_model == h.canonical_model(o.gpu_model) and o.availability == 'unknown' for o in offers)
        assert all(o.tier == ('B' if cls == h.HetznerCollector else 'A') for o in offers)
        assert all(o.export_allowed is (cls != h.HetznerCollector) for o in offers)
        assert store.upsert(offers) == len(offers)
        assert store.upsert(offers) == 0
        count = store.connection.execute('SELECT COUNT(*) FROM meta').fetchone()[0]
        assert c.collect() == [] and c.skipped and c.pages_fetched == 0
        assert store.connection.execute('SELECT COUNT(*) FROM meta').fetchone()[0] == count


@pytest.mark.parametrize('cls', CLASSES)
def test_failure_consumes_persisted_budget_and_no_partial_batch(cls, tmp_path):
    path = tmp_path/'prices.sqlite'
    calls = []
    def fail(url):
        calls.append(url)
        raise URLError('synthetic outage')
    with PriceStore(path) as s:
        c = collector(cls, s, fetch=fail)
        with pytest.raises(OSError): c.collect()
        assert len(calls) == c.pages_fetched == 1
        assert not s.latest_prices()
    with PriceStore(path) as s:
        assert collector(cls, s, fetch=fail).collect() == []
        assert len(calls) == 1
        future = (parse_utc(STAMP)+timedelta(hours=cls.interval_hours)).isoformat()
        c = collector(cls, s, fetch=fail, clock=lambda:future)
        with pytest.raises(OSError): c.collect()
        assert len(calls) == 2


def test_aws_hardware_and_comparison_scope():
    offers = rows(h.AwsCollector)
    direct = {o.sku:o for o in offers if o.price_type == 'on_demand'}
    h100 = direct['p5.48xlarge']
    assert h100.gpu_count == 8 and h100.price_per_hour == 55.04
    assert h100.price_per_gpu_hour == 6.88 and h100.gpu_model == 'H100 80GB'
    assert h100.effective_from == '2026-11-01T00:00:00Z'
    assert direct['g4dn.xlarge'].gpu_model == 'T4 16GB'
    assert direct['g6.xlarge'].gpu_model == 'L4 24GB'
    assert all(not o.sku.startswith(('g6f.', 'gr6f.', 'inf', 'trn')) for o in offers)
    assert all(o.price_per_hour > 0 for o in offers)  # Capacity Block zero rows excluded.
    spot = [o for o in offers if o.price_type == 'spot']
    assert spot and all(o.is_from_price and not eligible(o) and o.zone is None for o in spot)
    assert len({o.region for o in spot}) == 4
    assert all(o.tax_basis == 'excluded' for o in offers)


def test_aws_refuses_changed_header_truncated_rows_and_region():
    with pytest.raises(ValueError): list(h.aws_rows(['bad CSV\n']*12))
    with pytest.raises(ValueError): list(h.aws_rows(blob('aws.csv').splitlines(keepends=True)[:6]+['"truncated"\n']))
    raw = next(h.aws_rows(blob('aws.csv').splitlines(keepends=True)))
    with pytest.raises(ValueError): h.aws_normalize(raw, STAMP, h.AWS_INDEX, 'eu-west-1')
    with pytest.raises(ValueError): h.AwsCollector(store=None, regions=['us-east-1']*2)


def test_real_aws_h200_memory_is_per_gpu_not_instance_total():
    raw=next(r for r in h.aws_rows(blob('aws.csv').splitlines(keepends=True)) if r['Instance Type']=='p5en.48xlarge')
    o=h.aws_normalize(raw,STAMP,h.AWS_INDEX,'us-east-1')
    assert raw['GPU Memory']=='141 GB HBM3e' and o.vram_raw==raw['GPU Memory']
    assert o.gpu_model=='H200 141GB' and o.gpu_count==8
    assert o.price_per_gpu_hour==pytest.approx(63.296/8)
    raw['GPU Memory']='1128 GB HBM3e'  # Official documented total representation.
    assert h.aws_normalize(raw,STAMP,h.AWS_INDEX,'us-east-1').gpu_model=='H200 141GB'
    raw['GPU Memory']='140 GB HBM3e'
    with pytest.raises(ValueError):h.aws_normalize(raw,STAMP,h.AWS_INDEX,'us-east-1')


def test_independent_audit_refuses_eightfold_h200_vram_error(tmp_path):
    o=next(o for o in rows(h.AwsCollector) if o.sku=='p5en.48xlarge' and o.price_type=='on_demand')
    with PriceStore(tmp_path/'bad.sqlite') as s:
        s.upsert([replace(o,gpu_model='H200 17.625GB')])
        with pytest.raises(ValueError,match='H200 per-GPU memory must match official specifications'):
            build_site(s,tmp_path/'bad-preview',site_base_url='https://gpu-cloud-prices.pages.dev',built_at=STAMP,today=STAMP[:10])


def test_aws_page_limit_preserves_limited_state_and_stops_spot():
    with PriceStore(':memory:') as s:
        c = collector(h.AwsCollector, s)
        offers = c.collect(limit_pages=2)
        assert offers and not c.complete and c.pages_fetched == 2
        assert all(o.price_type == 'on_demand' for o in offers)


def test_real_spot_duplicates_coalesce_and_conflicting_prices_fail_closed():
    data=fetch(h.AWS_SPOT)
    groups=[g for r in data['config']['regions'] for g in r['instanceTypes']]
    repeated=[s for g in groups for s in g['sizes'] if s['size']=='g6.8xlarge']
    assert len(repeated)>len({r['region'] for r in data['config']['regions']})
    offers=rows(h.AwsCollector)
    assert len({(o.sku,o.region,o.price_type) for o in offers})==len(offers)
    target=next(g for g in groups if sum(s['size']=='g6.8xlarge' for s in g['sizes'])>1)
    dup=[s for s in target['sizes'] if s['size']=='g6.8xlarge'][1]
    next(c for c in dup['valueColumns'] if c['name']=='linux')['prices']['USD']='999'
    with PriceStore(':memory:') as s:
        c=collector(h.AwsCollector,s,fetch=lambda u:data if u==h.AWS_SPOT else fetch(u))
        with pytest.raises(ValueError,match='conflicting duplicate'):c.collect()


def test_oracle_gpu_hour_is_multiplied_and_no_ocpu_as_vcpu():
    offers = rows(h.OracleCollector)
    o = next(x for x in offers if x.product == 'BM.GPU.H100.8')
    assert o.price_per_hour == 80 and o.price_per_gpu_hour == 10
    assert o.source_billing_unit == 'GPU-hour' and o.price_scope == 'instance'
    assert o.vcpu is None and o.cpu_raw == '112 OCPUs' and o.ram_gb is None
    assert o.region is None and o.tax_basis == 'unknown'
    assert len(offers) == len(h.ORACLE_SHAPES) == 8
    assert all('VMware' not in o.gpu_model_raw and 'Customer' not in o.gpu_model_raw for o in offers)


@pytest.mark.parametrize('change', ['metric','missing','paging','duplicate'])
def test_oracle_fails_closed_on_contract_change(change):
    data = fetch(h.ORACLE)
    target = next(r for r in data['items'] if r['partNumber'] == 'B98415')
    if change == 'metric': target['metricName'] = 'Node Per Hour'
    if change == 'missing': data['items'].remove(target)
    if change == 'paging': data['hasMore'] = True
    if change == 'duplicate': data['items'].append(deepcopy(target))
    with PriceStore(':memory:') as s:
        with pytest.raises(ValueError): collector(h.OracleCollector,s,fetch=lambda u:data).collect()


def test_ovh_exact_microcents_whole_instance_and_exclusions():
    offers = rows(h.OvhCollector)
    o = next(x for x in offers if x.sku == 'h100-760.consumption')
    assert o.currency == 'EUR' and o.price_per_hour == 5.6 and o.price_per_gpu_hour == 2.8
    assert o.gpu_count == 2 and o.tax_basis == 'excluded' and o.region is None
    assert o.minimum_billing_seconds == 60
    assert all('win-' not in o.sku and 'monthly' not in o.sku and 'ai-' not in o.sku for o in offers)
    rates={'source':FX_URL,'date':'2026-11-12','rates':{'EUR':1.,'USD':1.1}}
    usd=to_usd(o,rates)
    assert usd.price_per_gpu_hour == pytest.approx(3.08)
    assert usd.original_price == 5.6 and usd.fx_rate == 1.1 and usd.fx_source == FX_URL
    assert to_usd(o,None).currency == 'EUR'


@pytest.mark.parametrize('change',['currency','unit','promotion','money'])
def test_ovh_refuses_currency_unit_promotion_and_money_drift(change):
    data=fetch(h.OVH)
    r=next(r for r in data['addons'] if r['planCode']=='h100-760.consumption')
    if change=='currency':data['locale']['currencyCode']='USD'
    if change=='unit':r['pricings'][0]['intervalUnit']='month'
    if change=='promotion':r['pricings'][0]['promotions']=[{'discount':.5}]
    if change=='money':r['pricings'][0]['price']=5.6
    with PriceStore(':memory:') as s:
        with pytest.raises(ValueError):collector(h.OvhCollector,s,fetch=lambda u:data).collect()


def test_vultr_explicit_hardware_and_deployment_flags():
    offers=rows(h.VultrCollector)
    assert len(offers)==13 and all(o.region is None for o in offers)
    o=next(o for o in offers if '8-h100' in o.sku)
    assert o.price_type=='interruptible' and o.price_per_gpu_hour==2.3
    assert not any('8-h100' in o.sku and o.price_type=='on_demand' for o in offers)
    l40=[o for o in offers if o.sku.startswith('vcg-')]
    assert len(l40)==3 and all(o.gpu_model=='L40S 48GB' for o in l40)
    assert l40[2].gpu_count==4 and l40[2].price_per_gpu_hour==1.671
    assert all(o.tax_basis=='unknown' for o in offers)


def test_vultr_cursor_pagination_and_cycle_detection():
    original=fetch(h.VULTR+'plans')
    def page(u):
        if 'plans-metal' in u:return fetch(u)
        p=deepcopy(original)
        p['plans']=original['plans'][1:] if 'cursor=' in u else original['plans'][:1]
        p['meta']['links']['next']='token' if 'cursor=' not in u else ''
        return p
    with PriceStore(':memory:') as s:
        c=collector(h.VultrCollector,s,fetch=page)
        assert len(c.collect())==13 and c.pages_fetched==3
    bad=deepcopy(original);bad['plans']=[];bad['meta']['links']['next']='loop'
    with PriceStore(':memory:') as s:
        with pytest.raises(ValueError):collector(h.VultrCollector,s,fetch=lambda u:bad).collect()


def test_vultr_unknown_regional_price_schema_cannot_silently_use_base():
    data=fetch(h.VULTR+'plans');data['plans'][0]['location_cost']={'ams':{'hourly_cost':2}}
    with PriceStore(':memory:') as s:
        with pytest.raises(ValueError):collector(h.VultrCollector,s,fetch=lambda u:data).collect()


def test_hetzner_hourly_setup_cap_regions_and_from_price():
    offers=rows(h.HetznerCollector)
    assert len(offers)==5
    o=next(o for o in offers if o.sku=='GEX430')
    assert o.gpu_count==4 and o.price_per_hour==5.7676 and o.price_per_gpu_hour==1.4419
    assert o.region=='HEL1' and o.tax_basis=='excluded' and not o.export_allowed
    assert '3,599.00' in o.price_conditions and 'one-time setup' in o.price_conditions
    assert {o.region for o in offers if o.sku=='GEX131'}=={'HEL1','FSN1'}
    assert all(o.is_from_price for o in offers if o.sku=='GEX131')
    assert all(region_class(o.region)=='standard' for o in offers)


def test_hetzner_rejects_tax_unit_and_truncated_cards():
    for text in [blob('hetzner.html').replace('excl. VAT','incl. VAT'), blob('hetzner.html')[:500]]:
        with PriceStore(':memory:') as s:
            with pytest.raises(ValueError):collector(h.HetznerCollector,s,fetch=lambda u:text).collect()


def test_http_bulk_stream_is_allowlisted_and_bounded():
    url='https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/20261110173852/us-east-1/index.csv'
    class Response(BytesIO):
        headers={}
        def geturl(self):return url
    client=http.HttpClient(opener=lambda *a,**k:Response(b'a\nb\n'))
    assert list(client.iter_lines(url,max_bytes=4))==['a\n','b\n']
    with pytest.raises(ValueError):list(client.iter_lines(url,max_bytes=3))
    for u in [url.replace('AmazonEC2','AmazonS3'),url.replace('index.csv','../../index.csv'),
              url.replace('pricing.us-east-1.amazonaws.com','evil.example'),h.ORACLE]:
        with pytest.raises(ValueError):list(client.iter_lines(u))


def test_budgeted_http_does_not_retry():
    calls=[]
    def fail(*a,**k):calls.append(1);raise URLError('failure')
    client=http.HttpClient(opener=fail,sleep=lambda x:pytest.fail('unexpected retry'))
    for u in [h.ORACLE,h.OVH,h.VULTR+'plans',h.AWS_SPOT,h.HETZNER]:
        with pytest.raises(OSError):client.get_bytes(u)
    assert len(calls)==5


def test_new_providers_eight_languages_audited_and_b_display_only(tmp_path):
    with PriceStore(tmp_path/'test.sqlite') as store:
        for cls in CLASSES:
            store.upsert(collector(cls,store).collect())
            store.record_run(provider=cls.name,started_at=STAMP,status='complete',pages=1,records=1)
        out=tmp_path/'preview'
        result=build_site(store,out,site_base_url='https://gpu-cloud-prices.pages.dev',built_at=STAMP,today=STAMP[:10])
        assert result['offers_today'] > 40
        assert audit_site(store.connection,out)['status']=='passed'
        catalogs=load_catalogs()
        for lang in LANGUAGES:
            text=(out/lang.prefix/'methodology.html').read_text(encoding='utf-8')
            for key in ['aws','oracle','ovh','vultr','hetzner','gcp','alibaba_intl','tencent_intl']:
                assert catalogs[lang.code].html('note_'+key) in text
                assert PROVIDERS[key]['name'] in text
        for p in (out/'data').glob('*.json'):
            data=json.loads(p.read_text(encoding='utf-8'))
            assert all(o['provider']!='hetzner' and o['tier']=='A' for o in data.get('offers',[]))
        # Source arithmetic is independently recomputed by the normal audit.
        p=out/'data/h100-80gb.json';data=json.loads(p.read_text(encoding='utf-8'))
        data['offers'][0]['price_per_gpu_hour'] += 1
        p.write_text(json.dumps(data),encoding='utf-8')
        with pytest.raises(ValueError):audit_site(store.connection,out)


def test_blocked_providers_and_runtime_optional_gcp_are_visible():
    assert PROVIDERS['gcp']['tier']=='A' and PROVIDERS['gcp']['enabled']
    for name in ['alibaba_intl','tencent_intl']:
        assert PROVIDERS[name]['tier']=='D' and not PROVIDERS[name]['enabled']
        with pytest.raises(ValueError):http.validate_url(PROVIDERS[name]['source'])


def test_long_catalog_skips_preserve_existing_ci_gate_contract(tmp_path,monkeypatch,capsys):
    from ci.collection_gate import classify
    from pipelines.gpu_prices import run
    monkeypatch.setattr(run,'configure_logging',lambda:None)
    monkeypatch.setattr(run,'AwsCollector',lambda store:collector(h.AwsCollector,store,clock=h.utc_now))
    class Azure:
        complete=True
        pages_fetched=raw_items=1
        def collect(self,**kw):
            return [replace(rows(h.OracleCollector)[0],provider='azure',sku='ci-contract-fixture',
                            source_url='https://prices.azure.com/api/retail/prices',collected_at=h.utc_now())]
    monkeypatch.setattr(run,'AzureCollector',Azure)
    args=['collect','--db',str(tmp_path/'cli.sqlite'),'--provider','azure','--provider','aws']
    assert run.main(args)==0
    assert classify(json.loads(capsys.readouterr().out),0)[0]
    assert run.main(args)==0
    data=json.loads(capsys.readouterr().out)
    assert 'aws' not in data['collections']
    assert data['catalog_skips']['aws']['reason']=='source attempted within 24 hours'
    assert data['catalog_skips']['aws']['pages']==0 and classify(data,0)[0]
    # A manual new-provider-only repeat remains a successful, honest no-op.
    assert run.main(['collect','--db',str(tmp_path/'cli.sqlite'),'--provider','aws'])==0
    data=json.loads(capsys.readouterr().out)
    assert data['collections']=={} and data['collection']['status']=='skipped'


def test_legacy_hourly_skips_keep_the_exact_ci_wire_reason(tmp_path,monkeypatch,capsys):
    from ci.collection_gate import classify
    from pipelines.gpu_prices import run
    from pipelines.gpu_prices.providers.page_collector import PageCollector
    content=(ROOT.parent/'provider_pages/featurize.html').read_bytes()
    monkeypatch.setattr(run,'configure_logging',lambda:None)
    monkeypatch.setattr(run,'refresh',lambda p:(None,'unavailable'))
    monkeypatch.setattr(run,'PageCollector',lambda name,store:PageCollector(name,store=store,
        client=SimpleNamespace(get_bytes=lambda *a,**kw:content)))
    args=['collect','--db',str(tmp_path/'cli.sqlite'),'--provider','featurize']
    assert run.main(args)==0
    capsys.readouterr()
    assert run.main(args)==0
    data=json.loads(capsys.readouterr().out)
    assert data['collections']['featurize']['reason']=='page fetched within 60 minutes'
    assert classify(data,0)[0]
