"""b17: real public fixtures, separate monthly units, optional secret isolation."""
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import traceback
from urllib.error import HTTPError, URLError

import pytest

from pipelines.gpu_prices import http, links
from pipelines.gpu_prices.audit import AuditError, audit_site
from pipelines.gpu_prices.build_site import build_site
from pipelines.gpu_prices.clore_auth import CloreRequestError
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.monthly import MonthlyCollector, monthly_quotes
from pipelines.gpu_prices.providers import clore
from pipelines.gpu_prices.providers.page_collector import PageCollector, normalize, PARSERS
from pipelines.gpu_prices.providers.pages import ParseError, NotSupported, massedcompute, cloudclusters
from pipelines.gpu_prices.providers.registry import PROVIDERS
from pipelines.gpu_prices.store import PriceStore

FIX = Path(__file__).parent/'fixtures'/'affiliates'
STAMP = '2026-11-12T21:30:00Z'
BUILT = '2026-11-12T21:31:00Z'
NEW = ('novita','cloudclusters','gpuoncloud','massedcompute','hyperbolic')


def source(name):
    return (FIX/(name+'.html')).read_text(encoding='utf-8')


@pytest.mark.parametrize('name,module,count',[('massedcompute',massedcompute,74),('cloudclusters',cloudclusters,12)])
def test_live_fixture_identity_and_real_rows(name,module,count):
    meta=json.loads((FIX/(name+'.fetch.json')).read_text(encoding='utf-8'))
    data=(FIX/(name+'.html')).read_bytes()
    assert meta['status']==200 and meta['method']=='GET' and meta['url']==module.URLS[0]
    assert hashlib.sha256(data).hexdigest()==meta['fixture_sha256']
    assert len(data)==meta['fixture_bytes'] and len(module.parse(data.decode(),STAMP))==count


def test_massed_multigpu_exact_values_canonical_names_and_unknowns():
    rows=[normalize(r) for r in massedcompute.parse(source('massedcompute'),STAMP)]
    assert len(rows)==74
    assert all(o.tier=='C' and not o.export_allowed and o.region is None and o.tax_basis=='unknown' for o in rows)
    h=next(o for o in rows if o.product=='H100 (80GB)' and o.gpu_count==4)
    assert (h.price_per_hour,h.price_per_gpu_hour,h.vcpu,h.ram_raw)==(10.92,2.73,64,'512 GB')
    assert h.gpu_model=='H100 80GB' and h.ram_gb is None
    assert not any(o.product=='H100 (80GB)' and o.gpu_count==8 for o in rows)
    assert min(o.price_per_gpu_hour for o in rows if o.gpu_model.startswith('A100'))==1.35
    assert 'H100 SXM 80GB' in {o.gpu_model for o in rows}
    assert 'RTX 6000 Ada 48GB' in {o.gpu_model for o in rows}


@pytest.mark.parametrize('name,module,old,new',[
    ('massedcompute',massedcompute,'/hr','/mo'),
    ('massedcompute',massedcompute,'>Qty<','>GPU allocation<'),
    ('massedcompute',massedcompute,'$2.73','$NaN'),
    ('cloudclusters',cloudclusters,'/mo','/hr'),
    ('cloudclusters',cloudclusters,'price-item-active','changed-active'),
    ('cloudclusters',cloudclusters,'Single-GPU configurations','Configurations'),
    ('cloudclusters',cloudclusters,'359.00','0.00'),
])
def test_unit_structure_and_price_drift_refused(name,module,old,new):
    assert old in source(name)
    with pytest.raises(ParseError):module.parse(source(name).replace(old,new),STAMP)


def test_cloudclusters_monthly_commitment_is_never_hourly():
    rows=cloudclusters.parse(source('cloudclusters'),STAMP)
    r=next(r for r in rows if r['gpu_model']=='RTX 4090 24GB')
    assert (r['price_per_month'],r['commitment_months'],r['region'],r['is_promotion'])==(359,24,'USA',True)
    assert r['gpu_count']==1 and r['tax_basis']=='unknown'
    assert all('price_per_hour' not in r and 'price_per_gpu_hour' not in r for r in rows)
    assert next(r for r in rows if r['gpu_model'].startswith('K80'))['gpu_count'] is None


@pytest.mark.parametrize('name,fail',[('massedcompute',False),('massedcompute',True),('cloudclusters',False),('cloudclusters',True)])
def test_hourly_budget_shared_persistent_and_failed_request_consumes_it(tmp_path,name,fail):
    calls=[]
    def get(url,**kwargs):
        calls.append(url)
        if fail:raise HTTPError(url,403,'Forbidden',{},None)
        return source(name).encode()
    path=tmp_path/'price.sqlite'
    def make(store,stamp):
        args=dict(store=store,client=SimpleNamespace(get_bytes=get),clock=lambda:stamp)
        return MonthlyCollector(**args) if name=='cloudclusters' else PageCollector(name,**args)
    with PriceStore(path) as store:
        first=make(store,STAMP)
        if fail:
            with pytest.raises(HTTPError):first.collect()
        else:
            offers=first.collect();store.upsert(offers)
            if name=='cloudclusters':assert first.save()==12
    with PriceStore(path) as store:
        second=make(store,'2026-11-12T22:29:59Z');assert second.collect()==[] and second.skipped
        third=make(store,'2026-11-12T22:30:00Z')
        if fail:
            with pytest.raises(HTTPError):third.collect()
        else:third.collect()
    assert len(calls)==2


@pytest.mark.parametrize('key',NEW)
def test_pending_links_stay_plain_and_can_activate_by_config_only(tmp_path,monkeypatch,key):
    config=json.loads(links.AFFILIATES_CONFIG.read_text(encoding='utf-8'))
    assert config[key]['status']=='pending'
    assert not {'ref_id','affiliate_url','link_template','ref_param'} & config[key].keys()
    monkeypatch.setenv('AFFILIATE_'+key.upper()+'_REF','unjoined-id')
    plain=links.link_for(key,'','','')
    assert plain['url']==PROVIDERS[key]['source'] and not plain['is_affiliate']
    path=tmp_path/'affiliates.json';monkeypatch.setattr(links,'AFFILIATES_CONFIG',path)
    config[key].update(status='active',ref_id='real-owner-id',affiliate_url='https://affiliate.example/r?owner=real-owner-id')
    path.write_text(json.dumps(config),encoding='utf-8')
    active=links.link_for(key,'','','');assert active['is_affiliate'] and active['url']==config[key]['affiliate_url']
    config[key]['status']='pending';path.write_text(json.dumps(config),encoding='utf-8')
    assert not links.link_for(key,'','','')['is_affiliate']


def test_d_sources_have_no_collector_or_http_endpoint():
    for key in ('novita','gpuoncloud','hyperbolic'):
        assert PROVIDERS[key]['tier']=='D' and PROVIDERS[key]['link_only'] and not PROVIDERS[key]['enabled']
        assert key not in PARSERS
        with pytest.raises(ValueError):http.validate_url(PROVIDERS[key]['source'])


class Response:
    def __init__(self,url=clore.API,data=b'{"code":0,"servers":[]}'):
        self.url,self.data=url,data
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self):return self.data
    def geturl(self):return self.url


@pytest.mark.parametrize('key',['','synthetic-private-key'])
@pytest.mark.parametrize('transport',['shared','standalone'])
def test_clore_optional_auth_exact_header_and_no_secret_to_other_hosts(monkeypatch,key,transport):
    monkeypatch.setenv('CLORE_API_KEY',key);calls=[]
    def opener(req,timeout):
        calls.append(req);return Response(req.full_url)
    if transport=='shared':http.HttpClient(opener=opener).get_json(clore.API)
    else:
        monkeypatch.setattr(clore,'build_opener',lambda *a:SimpleNamespace(open=opener));clore.fetch_json(clore.API)
    assert calls[0].get_header('Auth')==(key or None)
    assert calls[0].get_header('Authorization') is None
    http.HttpClient(opener=opener).get_json('https://api.verda.com/v1/instance-types')
    assert calls[-1].get_header('Auth') is None


@pytest.mark.parametrize('kind',['http401','http403','http500','network','invalid_json','redirect','runtime'])
@pytest.mark.parametrize('transport',['shared','standalone'])
def test_clore_error_body_headers_and_exceptions_never_leak_key(monkeypatch,caplog,kind,transport):
    secret='synthetic-private-key-do-not-print'
    monkeypatch.setenv('CLORE_API_KEY',secret)
    def opener(req,timeout):
        if kind.startswith('http'):raise HTTPError(req.full_url,int(kind[4:]),secret,{'Auth':secret},io.BytesIO(secret.encode()))
        if kind=='network':raise URLError(secret)
        if kind=='runtime':raise RuntimeError(secret)
        if kind=='redirect':raise ValueError(secret)
        return Response(data=(secret+' invalid json').encode())
    monkeypatch.setattr(clore.time,'sleep',lambda *a:None)
    if transport=='shared':call=lambda:http.HttpClient(opener=opener).get_json(clore.API)
    else:
        monkeypatch.setattr(clore,'build_opener',lambda *a:SimpleNamespace(open=opener));call=lambda:clore.fetch_json(clore.API)
    with pytest.raises(CloreRequestError) as error:call()
    assert secret not in str(error.value)+repr(error.value)+''.join(traceback.format_exception(error.value))+caplog.text


def test_clore_unauthenticated_401_cli_is_clear_without_traceback(tmp_path,monkeypatch,capsys,caplog):
    from pipelines.gpu_prices import run
    from pipelines.gpu_prices.providers.clore_collector import CloreCollector
    monkeypatch.delenv('CLORE_API_KEY',raising=False)
    def opener(req,timeout):raise HTTPError(req.full_url,401,'unauthorized',{},io.BytesIO(b'private error body'))
    monkeypatch.setattr(run,'configure_logging',lambda:None)
    monkeypatch.setattr(run,'CloreCollector',lambda **kw:CloreCollector(client=http.HttpClient(opener=opener),**kw))
    assert run.main(['collect','--db',str(tmp_path/'p.sqlite'),'--provider','clore'])==1
    out=capsys.readouterr().out
    assert 'set CLORE_API_KEY secret' in out+caplog.text
    assert 'Traceback' not in caplog.text and 'private error body' not in out+caplog.text


@pytest.fixture(scope='module')
def affiliate_site(tmp_path_factory):
    path=tmp_path_factory.mktemp('affiliate_site');db=path/'prices.sqlite';out=path/'site'
    with PriceStore(db) as store:
        store.upsert([normalize(r) for r in massedcompute.parse(source('massedcompute'),STAMP)])
        c=MonthlyCollector(store=store,client=SimpleNamespace(get_bytes=lambda *a,**k:source('cloudclusters').encode()),clock=lambda:STAMP)
        c.collect();c.save()
        build_site(store,out,site_base_url='https://gpu-cloud-prices.pages.dev',today='2026-11-12',built_at=BUILT)
    return db,out


def test_affiliate_providers_and_monthly_prices_in_all_eight_languages(affiliate_site):
    db,out=affiliate_site
    assert audit_site(db,out)['status']=='passed'
    for lang in LANGUAGES:
        content=(out/(lang.prefix+'/' if lang.prefix else '')/'index.html').read_text(encoding='utf-8')
        assert content.count('data-monthly-quote=')==12 and '359.00' in content
        assert load_catalogs()[lang.code].text('monthly_policy') in content
        for key in ('novita','gpuoncloud','hyperbolic'):
            assert 'data-provider-link="'+key+'"' in content
        assert 'data-provider-link="cloudclusters"' in content and 'nofollow noopener' in content
    assert [p.name for p in (out/'data').glob('*.json')] == ['latest.json']
    assert json.loads((out/'data/latest.json').read_text(encoding='utf-8'))['exports'] == []


@pytest.mark.parametrize('old,new',[('$359.00','$0.01'),('>24</span>','>1</span>'),('nofollow noopener','sponsored nofollow noopener'),('data-monthly-quote="0"','data-monthly-quote="999"')])
def test_audit_detects_monthly_price_term_and_affiliate_corruption(affiliate_site,old,new):
    db,out=affiliate_site;p=out/'index.html';content=p.read_text(encoding='utf-8');assert old in content
    try:
        p.write_text(content.replace(old,new),encoding='utf-8')
        with pytest.raises(AuditError):audit_site(db,out)
    finally:p.write_text(content,encoding='utf-8')


def test_massed_historical_403_is_still_refused():
    with pytest.raises(NotSupported):massedcompute.parse('<html>Access denied</html>',STAMP)


@pytest.mark.parametrize('url',['http://affiliate.example/id','https://owner:pass@affiliate.example/id','javascript:alert(1)'])
def test_active_affiliate_urls_fail_closed(tmp_path,monkeypatch,url):
    path=tmp_path/'affiliates.json';path.write_text(json.dumps({'novita':{'status':'active','ref_id':'real','affiliate_url':url}}),encoding='utf-8')
    monkeypatch.setattr(links,'AFFILIATES_CONFIG',path)
    with pytest.raises(ValueError):links.link_for('novita','','','')


@pytest.mark.parametrize('url',[massedcompute.URLS[0],cloudclusters.URLS[0]])
@pytest.mark.parametrize('status',[403,429,503])
def test_page_endpoints_never_retry_inside_hourly_claim(url,status):
    calls=[]
    def opener(req,timeout):
        calls.append(req);raise HTTPError(url,status,'synthetic',{'Retry-After':'3600'},None)
    with pytest.raises(HTTPError):http.HttpClient(opener=opener,sleep=lambda s:pytest.fail('unexpected retry')).get_bytes(url)
    assert len(calls)==1


def test_monthly_asof_and_cli_counts_remain_separate_from_hourly(tmp_path,monkeypatch,capsys):
    from pipelines.gpu_prices import run
    path=tmp_path/'prices.sqlite'
    monkeypatch.setattr(run,'configure_logging',lambda:None)
    monkeypatch.setattr(run,'MonthlyCollector',lambda **kw:MonthlyCollector(client=SimpleNamespace(get_bytes=lambda *a,**k:source('cloudclusters').encode()),clock=lambda:STAMP,**kw))
    assert run.main(['collect','--provider','cloudclusters','--db',str(path)])==0
    result=json.loads(capsys.readouterr().out)['collections']['cloudclusters']
    assert (result['records'],result['monthly_records'],result['hourly_records'])==(12,12,0)
    with PriceStore(path) as store:
        assert store.observation_rows()==[]
        assert monthly_quotes(store.connection,'2026-11-12T21:29:59Z')==[]
        assert len(monthly_quotes(store.connection,BUILT))==12
        assert len(monthly_quotes(store.connection,'2026-11-12T21:30:00+00:00'))==12
        assert store.connection.execute("SELECT records FROM collection_runs WHERE provider='cloudclusters'").fetchone()[0]==12


def test_workflow_clore_secret_is_collect_step_only():
    text=(Path(__file__).resolve().parents[1]/'.github/workflows/gpu-site.yml').read_text(encoding='utf-8')
    assert text.count('CLORE_API_KEY: ${{ secrets.CLORE_API_KEY }}')==1
    step=text.split('- name: Collect prices and apply provider failure policy',1)[1].split('- name:',1)[0]
    assert 'env:\n          CLORE_API_KEY: ${{ secrets.CLORE_API_KEY }}' in step
