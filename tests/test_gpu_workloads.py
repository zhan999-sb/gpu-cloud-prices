"""Reviewed memory/software/capacity screens, billing, locales and SEO gates."""
from dataclasses import replace
import json
import re
import xml.etree.ElementTree as ET
import pytest

from pipelines.gpu_prices import workloads, workload_audit
from pipelines.gpu_prices.audit import AuditError, Page, _snapshot, audit_site
from pipelines.gpu_prices.build_site import build_site, model_slugs
from pipelines.gpu_prices.i18n import LANGUAGES, load_catalogs
from pipelines.gpu_prices.schema import Offer
from pipelines.gpu_prices.store import PriceStore
from pipelines.gpu_prices.workload_specs import WORKLOADS, SOURCES, route, WORKLOAD_CAPACITY, GPU_SOFTWARE, VLLM_ROCM_TARGETS

DAY='2026-11-13';STAMP=DAY+'T08:00:00Z';ORIGIN='https://gpu.example.test'
SMALL='llama-3-1-8b-4bit';MEDIUM='llama-3-1-8b-fp16';LARGE='llama-3-1-70b-fp16'


def offer(model='T4 16GB',sku='base',rate=.4,**changes):
    data=dict(provider='azure',sku=sku,gpu_model=model,gpu_count=1,vcpu=8,ram_gb=32,
        region='eastus',price_type='on_demand',currency='USD',price_per_hour=rate,
        price_per_gpu_hour=rate,source_url='https://example.test/observed',collected_at=STAMP,
        minimum_billing_seconds=60)
    data.update(changes)
    return Offer(**data)


def data_for(rows):
    result={}
    for r in rows:result.setdefault(r.gpu_model,{'offers':[]})['offers'].append(r.to_dict())
    return result


def population():
    return [offer(),offer(sku='spot',rate=.2,price_type='spot'),
        offer('RTX 4090 24GB','4090',.6),offer('L40S 48GB','l40s',1),
        offer('A100 80GB','a100-four',4,gpu_count=4,price_per_gpu_hour=1),
        offer('H200 141GB','h200',8,gpu_count=2,price_per_gpu_hour=4),
        offer('H200 141GB','h200-spot',3,price_type='spot'),
        offer('MI300X 192GB','mi300',5,gpu_count=4,price_scope='per_gpu'),
        offer('T4 16GB','per-gpu',.5,gpu_count=4,price_scope='per_gpu'),
        offer('L40S 48GB','display-only',.9,tier='C',export_allowed=False),
        offer('Radeon Pro V520 8GB','v520',.01),offer('Tesla M60 8GB','m60',.02),
        offer('V100 32GB','v100',.03),offer('T4G 16GB','arm',.1),
        offer('Future GPU 900GB','unreviewed',.001)]


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    out=tmp_path_factory.mktemp('workloads')
    store=PriceStore(':memory:');store.upsert(population())
    result=build_site(store,out,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
    yield store,out,result
    store.close()


def test_reviewed_rules_and_official_evidence():
    data=workloads.snapshot(data_for(population()))
    assert {s:d['required'] for s,d in data.items()}==pytest.approx({
        MEDIUM:20.273741824,SMALL:5.873741824,'llama-2-13b-fp16':34.5554432,
        'llama-2-13b-4bit':11.1554432,LARGE:170.68435456,
        'llama-3-1-70b-4bit':44.68435456,'whisper-large':10})
    assert WORKLOADS['llama-2-13b-fp16']['kv_heads']==40
    assert data['llama-2-13b-4bit']['kv']==pytest.approx(3.3554432)
    assert data[SMALL]['kv']==pytest.approx(1.073741824)
    assert VLLM_ROCM_TARGETS=={'gfx90a','gfx942','gfx950','gfx1100','gfx1101','gfx1200','gfx1201','gfx1150','gfx1151'}
    assert all(v['source'].startswith('https://') for v in WORKLOAD_CAPACITY.values())
    assert all(v['source'].startswith('https://') and v['architecture'] for v in GPU_SOFTWARE.values())
    assert all(SOURCES[s][1].startswith('https://') for spec in WORKLOADS.values() for s in spec['sources'])
    assert data==workload_audit.snapshot({'models':data_for(population())})


def test_locale_punctuation_and_reviewed_copy():
    catalogs=load_catalogs()
    for code,tr in catalogs.items():
        assert tr.label_separator==({'fr':' : ','zh-Hans':'：','ja':'：'}.get(code,': '))
        if code in ('zh-Hans','ja','ko'):
            assert '{parameters}B' in tr.messages['workload_weight_rule']
        if code in ('ja','ko'):
            assert all('LLM' in tr.messages['workload_'+s['key']+'_title'] for s in WORKLOADS.values() if s['kind']=='estimate')
    assert 'o par paramètre' in catalogs['fr'].messages['workload_weight_rule']
    assert catalogs['es'].messages['workload_check_memory'].startswith('Prueba')
    assert catalogs['es'].messages['workload_entry_intro'].startswith('Define')
    assert all('Spot' not in value for key,value in catalogs['ko'].messages.items() if key.startswith('workload_'))
    assert 'provedor' in catalogs['pt-BR'].messages['workload_check_stock']


@pytest.mark.parametrize('slug',[SMALL,MEDIUM,LARGE,'whisper-large'])
@pytest.mark.parametrize('delta',[-.001,0,.001])
def test_reviewed_capacity_inclusive_boundaries(monkeypatch,slug,delta):
    need=workloads.snapshot({})[slug]['required']
    monkeypatch.setitem(WORKLOAD_CAPACITY,'T4',dict(gb=need+delta,source='https://example.test/spec'))
    monkeypatch.setitem(GPU_SOFTWARE,'T4',GPU_SOFTWARE['T4 16GB'])
    rows=data_for([offer('T4')])
    d=workloads.snapshot(rows)[slug]
    assert bool(d['rows']['single']) is (delta>=0)
    assert d==workload_audit.snapshot({'models':rows})[slug]


@pytest.mark.parametrize('change',[
    dict(gpu_count=None),dict(gpu_count=.5,gpu_fraction=.5),dict(gpu_fraction=.5),
    dict(currency='EUR'),dict(price_scope='gpu_component'),dict(is_from_price=True),
    dict(tax_basis='included'),dict(region='chinaeast'),dict(region='attatlanta1'),
    dict(price_type='reserved'),dict(availability='out_of_stock'),dict(availability='unavailable'),
])
def test_ineligible_quotes_cannot_win_or_count(change):
    bad=replace(offer(rate=.001),**change)
    rows=data_for([offer(),bad]);d=workloads.snapshot(rows)[SMALL]
    assert d['count']==1 and d['offers']==1
    assert d['rows']['single'][0]['offer']['price_per_gpu_hour']==.4
    assert d==workload_audit.snapshot({'models':rows})[SMALL]


def test_capacity_spoof_mining_laptop_unknown_and_raw_overreport():
    rows=[offer(name,str(i),.001) for i,name in enumerate(('CMP 170HX 64GB','P104-100 8GB','RTX 4090 Laptop GPU 16GB','Unknown GPU 999GB','RTX 4090 48GB','RTX 4090 25.2GB'))]
    rows += [offer('RTX 4090 24GB','raw',.001,vram_raw='48GB'),offer('RTX 4090 24GB','raw-name',.001,gpu_model_raw='NVIDIA RTX 4090 48GB'),offer('RTX 4090 24GB','real',.6)]
    data=data_for(rows);d=workloads.snapshot(data)[MEDIUM]
    assert [r['offer']['sku'] for r in d['rows']['single']]==['real']
    assert workloads.snapshot(data)==workload_audit.snapshot({'models':data})


def test_reviewed_public_catalog_total_vram_is_not_per_gpu_overreport():
    rows=data_for([
        offer('A100 80GB','vultr-four',9.589,provider='vultr',gpu_count=4,price_per_gpu_hour=9.589/4,vram_raw='320 GB total'),
        offer('A100 80GB','aws-eight',27.44705,provider='aws',gpu_count=8,price_per_gpu_hour=27.44705/8,vram_raw='640 GB HBM2e'),
        offer('A100 80GB','bad-total',.01,provider='vultr',gpu_count=4,price_per_gpu_hour=.0025,vram_raw='640 GB total'),
        offer('A100 80GB','bad-model-label',.01,provider='vultr',gpu_count=4,price_per_gpu_hour=.0025,gpu_model_raw='A100 320GB'),
    ])
    d=workloads.snapshot(rows)[LARGE]
    assert {x['offer']['sku'] for x in d['rows']['multi']}=={'vultr-four','aws-eight'}
    assert d['heads']['multi']['on_demand'][1]['sku']=='vultr-four'
    assert d==workload_audit.snapshot({'models':rows})[LARGE]


def test_software_floor_and_arm_badges_are_separate_from_memory(monkeypatch):
    data=data_for(population());d=workloads.snapshot(data)
    assert {x['offer']['gpu_model'] for x in d[SMALL]['rows']['memory_only']}=={'Radeon Pro V520 8GB','Tesla M60 8GB','V100 32GB','T4G 16GB'}
    assert d[SMALL]['heads']['single']['on_demand'][1]['gpu_model']=='T4 16GB'
    assert d['whisper-large']['heads']['single']['on_demand'][1]['gpu_model']=='T4 16GB'
    assert 'T4G 16GB' in {x['offer']['gpu_model'] for x in d['whisper-large']['rows']['memory_only']}
    monkeypatch.delitem(GPU_SOFTWARE,'T4 16GB')
    d=workloads.snapshot(data)
    assert 'T4 16GB' in {x['offer']['gpu_model'] for x in d[SMALL]['rows']['memory_only']}
    assert d==workload_audit.snapshot({'models':data})


def test_tensor_parallel_divisibility_memory_and_instance_billing(monkeypatch):
    rows=[offer('A100 80GB','four',9.59,gpu_count=4,price_per_gpu_hour=9.59/4),
          offer('H200 141GB','two',12,gpu_count=2,price_per_gpu_hour=6),
          offer('A100 80GB','three',.3,gpu_count=3,price_per_gpu_hour=.1),
          offer('T4 16GB','eight',1,gpu_count=8,price_per_gpu_hour=.125)]
    data=data_for(rows);d=workloads.snapshot(data)[LARGE]
    assert not d['rows']['single']
    assert d['heads']['multi']['on_demand'][1]['sku']=='four'
    assert [x['offer']['sku'] for x in d['rows']['multi']]==['four','two']
    assert d==workload_audit.snapshot({'models':data})[LARGE]
    monkeypatch.setitem(WORKLOADS[LARGE],'heads',10)
    assert [x['offer']['sku'] for x in workloads.snapshot(data)[LARGE]['rows']['multi']]==['two']
    monkeypatch.setitem(WORKLOADS[LARGE],'kv_heads',3)
    assert not workloads.snapshot(data)[LARGE]['rows']['multi']


def test_equal_per_gpu_price_prefers_smaller_bill_then_fewer_gpus():
    rows=data_for([offer(sku='eight-first',rate=3.2,gpu_count=8,price_per_gpu_hour=.4),offer(sku='one-second',rate=.4)])
    d=workloads.snapshot(rows)[SMALL]
    assert d['rows']['single'][0]['offer']['sku']=='one-second'
    assert d['heads']['single']['on_demand'][1]['gpu_count']==1
    assert d==workload_audit.snapshot({'models':rows})[SMALL]


def test_minimum_per_gpu_and_cheapest_instance_are_both_visible():
    rows=data_for([offer(sku='eight-cheaper-rate',rate=2.4,gpu_count=8,price_per_gpu_hour=.3),offer(sku='one-lower-bill',rate=.4)])
    d=workloads.snapshot(rows)[SMALL]
    assert d['rows']['single'][0]['offer']['sku']=='eight-cheaper-rate'
    assert d['heads']['single']['on_demand'][1]['sku']=='one-lower-bill'
    assert len(d['cards'])==2


def test_duplicate_gate_checks_each_section_and_marks_looser():
    rows=data_for([offer('L40S 48GB'),offer('H200 141GB','h200',2),offer('MI300X 192GB','mi300',3)])
    d=workloads.snapshot(rows)
    assert d[SMALL]['count']>=3 and d[SMALL]['siblings'] and not d[SMALL]['indexable']
    assert all(d[x['slug']]['required']>d[SMALL]['required'] for x in d[SMALL]['siblings'])
    assert d==workload_audit.snapshot({'models':rows})


def test_all_locales_metadata_sources_links_breadcrumbs_and_sitemaps(site):
    store,out,result=site;catalogs=load_catalogs();data=workload_audit.snapshot(_snapshot(store.connection,DAY,STAMP))
    assert result['audit']['workload_pages']==56
    indexed={n.text for n in ET.parse(out/'sitemap.xml').findall('.//{*}loc')}
    for lang in LANGUAGES:
        localized={n.text for n in ET.parse(out/('sitemap-'+lang.code+'.xml')).findall('.//{*}loc')}
        linking=[Page((out/lang.prefix/path).read_text(encoding='utf-8')) for path in ('index.html','guides/index.html','guides/choose-gpu-cloud-llm-inference/index.html')]
        related_raw=(out/lang.prefix/'guides/choose-gpu-cloud-llm-inference/index.html').read_text(encoding='utf-8')
        related=re.findall(r'<h2 data-search="guide:related">.*?</h2><ul data-workload-related="true">(.*?)</ul>',related_raw,re.S)
        assert len(related)==1
        assert {lang.path(route(s)) for s in WORKLOADS}<={a.get('href') for tag,a in Page(related[0]).links if tag=='a'}
        descriptions=[];titles=[]
        for slug,spec in WORKLOADS.items():
            raw=(out/lang.prefix/(route(slug).lstrip('/')+'index.html')).read_text(encoding='utf-8');page=Page(raw)
            title=catalogs[lang.code].text('workload_'+spec['key']+'_title');titles.append(title)
            assert page.h1==[title] and spec['model'] in page.search_values['workload:intro']
            assert [s['@type'] for s in page.structured]==['Article','BreadcrumbList']
            assert len(page.structured[1]['itemListElement'])==3
            assert (page.metadata.get('robots')!='noindex')==data[slug]['indexable']
            assert (ORIGIN+lang.path(route(slug)) in indexed & localized)==data[slug]['indexable']
            desc=page.metadata['description'];descriptions.append(desc)
            assert desc==page.metadata['og:description']==page.metadata['twitter:description']==page.structured[0]['description']
            if lang.code not in ('zh-Hans','ja','ko'): assert len(desc)<=155
            hrefs={a.get('href') for tag,a in page.links if tag=='a'}
            assert {SOURCES[s][1] for s in spec['sources']}<=hrefs
            assert lang.path('/reports/2026-W46/') in hrefs
            assert all(lang.path(route(slug)) in {a.get('href') for tag,a in p.links if tag=='a'} for p in linking)
            assert len([a for tag,a in page.links if tag=='link' and a.get('hreflang')])==9
            assert '\ufffd' not in raw and '??' not in raw
        assert len(set(titles))==len(set(descriptions))==7
    english=(out/(route(SMALL).lstrip('/')+'index.html')).read_text(encoding='utf-8')
    assert 'Arm (aarch64) host' in english and 'non-CUDA' in english and 'below floor' in english
    assert 'one GPU’s memory passes, but this offer is sold only as an instance with 4 GPUs' in english
    assert 'USD $20.00/hr' in Page(english).text


def test_thin_guard_regional_duplicates_and_stale_data(tmp_path):
    with PriceStore(':memory:') as store:
        store.upsert([offer(sku=str(i),region='eastus' if i%2 else 'westus') for i in range(5)])
        out=tmp_path/'thin';result=build_site(store,out,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
        assert result['audit']['indexable_workloads']==0
        assert not any('/workloads/' in u for u in result['sitemap_urls'])
        assert workload_audit.snapshot(_snapshot(store.connection,DAY,STAMP))[SMALL]['count']==1
        store.record_run(provider='azure',started_at=STAMP,status='failed',pages=0,records=0)
        stale=tmp_path/'failed';build_site(store,stale,today=DAY,built_at=STAMP,site_base_url=ORIGIN)
        page=Page((stale/(route(SMALL).lstrip('/')+'index.html')).read_text(encoding='utf-8'))
        assert not page.cards and page.search_values['workload:empty_single']


def test_independent_audit_never_uses_renderer(site,monkeypatch):
    store,out,_=site
    def forbidden(*args,**kwargs): raise AssertionError('renderer is not an oracle')
    for name in ('snapshot','render','description','software','reviewed','rank','instance_rank'):
        monkeypatch.setattr(workloads,name,forbidden)
    assert audit_site(store.connection,out)['status']=='passed'


def test_workloads_receive_strict_multilingual_html_checks(site):
    from test_gpu_prices import assert_multilingual_site
    store,out,_=site
    assert_multilingual_site(out,ORIGIN,model_slugs(_snapshot(store.connection,DAY,STAMP)['models']))


def test_common_cheaper_gpu_wins_and_unreviewed_models_are_disclosed():
    rows=data_for([offer('A100 80GB',rate=1),offer('RTX A6000 48GB',rate=.3),offer('Future GPU 900GB',rate=.001)])
    d=workloads.snapshot(rows)
    assert d['llama-3-1-70b-4bit']['overall']['on_demand'][1]['gpu_model']=='RTX A6000 48GB'
    assert d[SMALL]['unreviewed']==['Future GPU']
    assert d==workload_audit.snapshot({'models':rows})


@pytest.mark.parametrize('model,capacity',[
    ('A30',24),('A40',48),('L40',48),('RTX A4000',16),('RTX A5000',24),('RTX A6000',48),
    ('RTX 6000 Ada',48),('RTX 3090',24),('RTX 3090 Ti',24),('RTX 4080',16),('RTX 4080 SUPER',16),
    ('RTX 4060 Ti 16GB',16),('RTX PRO 4500',32),('RTX PRO 6000 Blackwell Server Edition',96),
    ('RTX PRO 6000 Blackwell Workstation Edition',96),('RTX PRO 6000 Blackwell Max-Q Workstation Edition',96),
    ('B300',288),('GH200 96GB',96),('MI325X',256),('MI355X',288),
])
def test_required_gpu_families_have_reviewed_capacity_and_official_target(model,capacity):
    from pipelines.gpu_prices.workload_specs import capacity_spec,gpu_software
    row=offer(model).to_dict()
    assert workloads.reviewed(row)==capacity
    assert capacity_spec(model)['source'].startswith('https://')
    g=gpu_software(model)
    assert g['cc'] is not None or g['gfx']
    assert g['source'].startswith('https://')


def test_identity_lookup_preserves_lower_host_capacity_and_rejects_overreport():
    assert workloads.reviewed(offer('GTX 1050 2GB').to_dict())==2
    assert workloads.reviewed(offer('GTX 1050 3GB').to_dict())==3
    assert workloads.reviewed(offer('GTX 1050 4GB').to_dict()) is None
    rows=data_for([offer('B200 179GB',vram_raw='1432 GB HBM3e',provider='aws',gpu_count=8),
                   offer('RTX A6000 51GB',sku='bad',rate=.001),
                   offer('B300 SXM6',sku='no-capacity',rate=2)])
    d=workloads.snapshot(rows)
    assert {x['offer']['gpu_model']:x['vram'] for x in d[LARGE]['rows']['single']}=={'B200 179GB':179,'B300 SXM6':288}
    assert 'B300 SXM6' in {x['offer']['gpu_model'] for x in d['whisper-large']['rows']['memory_only']}
    assert d==workload_audit.snapshot({'models':rows})


@pytest.mark.parametrize('model,cpu,supported',[
    ('T4G 16GB','',False),('A10G 24GB','aarch64',False),('A100 80GB','aarch64',True),
    ('GH200 96GB','',True),('H100 80GB','arm64',True),('MI300X 192GB','aarch64',False),
])
def test_host_architecture_floor_on_llm_and_whisper(model,cpu,supported):
    row=offer(model,cpu_raw=cpu).to_dict()
    assert workloads.software(row,WORKLOADS[SMALL]) is supported
    assert workload_audit._support(row,WORKLOADS[SMALL]) is supported
    assert workloads.software(row,WORKLOADS['whisper-large']) is supported


def test_verda_totals_and_clore_concatenated_model_tokens():
    rows=data_for([
        offer('A100 SXM4 80GB','verda',2,provider='verda',gpu_count=4,price_per_gpu_hour=.5,vram_raw='320GB GPU RAM',gpu_model_raw='4x A100 SXM4 80GB'),
        offer('A100 SXM4 80GB','verda-two',2,provider='verda',gpu_count=2,price_per_gpu_hour=1,vram_raw='160GB GPU RAM',gpu_model_raw='2x A100 SXM4 80GB'),
        offer('A100 SXM4 80GB','bad-total',.01,provider='verda',gpu_count=2,vram_raw='320GB GPU RAM'),
        offer('A100 SXM4 40GB','clore-a100',.2,provider='clore',vram_raw='40 GB',gpu_model_raw='NVIDIA A100SXM440GB'),
        offer('A100 SXM4 40GB','bad-a100',.01,provider='clore',vram_raw='80 GB',gpu_model_raw='NVIDIA A100SXM440GB'),
        offer('V100 SXM2 32GB','clore-v100',.1,provider='clore',vram_raw='32 GB',gpu_model_raw='Tesla V100SXM232GB'),
    ])
    d=workloads.snapshot(rows)
    assert {x['offer']['sku'] for x in d[LARGE]['rows']['multi']}=={'verda'}
    assert workloads.reviewed(next(r for m in rows.values() for r in m['offers'] if r['sku']=='verda-two'))==80
    assert d['llama-3-1-70b-4bit']['offers']==2
    assert 'clore-a100' in {x['offer']['sku'] for x in d[SMALL]['rows']['single']}
    assert 'clore-v100' in {x['offer']['sku'] for x in d[SMALL]['rows']['memory_only']}
    assert d==workload_audit.snapshot({'models':rows})


def test_multi_requires_insufficient_per_gpu_memory_and_overall_prefers_single_gpu_fit():
    rows=data_for([offer('MI300X 192GB','bundle',48,gpu_count=8,price_per_gpu_hour=6),
                   offer('L40S 48GB','tp',6.68,gpu_count=4,price_per_gpu_hour=1.67)])
    d=workloads.snapshot(rows)
    # "Best" is the cheapest single-GPU fit even when a tensor-parallel instance bills less;
    # the cheaper tensor-parallel option stays visible in its own section.
    assert d[LARGE]['overall']['on_demand'][1]['sku']=='bundle'
    assert d[LARGE]['heads']['multi']['on_demand'][1]['sku']=='tp'
    only_tp=data_for([offer('L40S 48GB','tp',6.68,gpu_count=4,price_per_gpu_hour=1.67)])
    t=workloads.snapshot(only_tp)
    assert t[LARGE]['heads']['single']['on_demand'] is None
    assert t[LARGE]['overall']['on_demand'][1]['sku']=='tp'
    assert t==workload_audit.snapshot({'models':only_tp})
    assert d[LARGE]['heads']['single']['on_demand'][1]['sku']=='bundle'
    assert all(x['vram']<p['required'] for p in d.values() for x in p['rows']['multi'])
    assert not d[SMALL]['rows']['multi']
    assert workloads.software(offer('MI300X 192GB').to_dict(),WORKLOADS['whisper-large'])
    assert d==workload_audit.snapshot({'models':rows})


def test_primary_sibling_cluster_has_one_strictest_reference_and_same_floor():
    rows=data_for([offer(model,sku=model,gpu_count=4) for model in ('T4 16GB','A30 24GB','A100 40GB','L40S 48GB','H200 141GB','MI300X 192GB')])
    d=workloads.snapshot(rows)
    assert d[SMALL]['siblings']==[dict(slug='llama-2-13b-4bit',sections=['single'])]
    for slug in ('llama-2-13b-4bit',MEDIUM,'llama-2-13b-fp16','llama-3-1-70b-4bit',LARGE,'whisper-large'): assert d[slug]['indexable']
    for page in d.values():
        for s in page['siblings']: assert not d[s['slug']]['siblings']
    assert d==workload_audit.snapshot({'models':rows})


def test_localized_headline_units_precision_and_number_literals(site):
    _,out,_=site
    for code,tr in load_catalogs().items():
        assert not any(re.search(r'\d,\d{3}',v) for k,v in tr.messages.items() if k.startswith('workload_'))
        for slug,spec in WORKLOADS.items():
            raw=(out/tr.language.prefix/(route(slug).lstrip('/')+'index.html')).read_text(encoding='utf-8')
            page=Page(raw)
            assert page.search_values['workload:unreviewed-models']=='Future GPU'
            assert '<details data-workload-unreviewed="true">' in raw
            lines=[v for k,v in page.search_values.items() if k.startswith('workload:best:')]
            assert all('1 GPUs' not in v for v in lines)
            if code in ('zh-Hans','ja','ko'): assert all(tr.text('workload_gpus') not in v for v in lines)
            assert 'INT4' not in page.metadata['description']
            assert 'Whisper large ' + tr.label_separator not in page.metadata['description']
            if code in ('de','fr','es','pt-BR') and spec.get('bits')==4: assert '4-bit' not in page.h1[0]
            if spec['kind']!='estimate':
                assert 'workload:tp_rule' not in page.search_values and 'workload:check_interconnect' not in page.search_values


def test_audit_policy_cannot_be_disabled_by_manifest(site):
    store,out,_=site;p=out/'gpu-prices-manifest.json';original=p.read_text(encoding='utf-8')
    try:
        manifest=json.loads(original);manifest['include_workloads']=False
        p.write_text(json.dumps(manifest),encoding='utf-8')
        with pytest.raises(AuditError,match='workload coverage required'): audit_site(store.connection,out)
    finally: p.write_text(original,encoding='utf-8')


@pytest.mark.parametrize('damage',[
    'rule','price','count','capacity','row','source','source-label','guide-link','report-link',
    'hidden','css','article','table-units','anchor','tax','instance','billing','unmarked','wording','explanation',
    'headroom','floor','badge','description','duplicate','unreviewed',
])
def test_independent_audit_rejects_corruption(site,damage):
    store,out,result=site;slug=SMALL;filename=route(slug).lstrip('/')+'index.html'
    raw=(out/filename).read_text(encoding='utf-8')
    changes={
        'rule':('data-format="decimal3">5.874<','data-format="decimal3">5.875<'),
        'price':('data-format="money">$0.400<','data-format="money">$0.001<'),
        'count':('/count" data-format="number">','/count" data-format="number">9'),
        'capacity':('/vram" data-format="number">16<','/vram" data-format="number">14<'),
        'source':('href="https://huggingface.co/blog/llama31"','href="https://example.test/fake"'),
        'source-label':('Hugging Face: Llama 3.1 weights and KV tables','Invented source'),
        'guide-link':('href="/guides/per-gpu-hour-vs-instance/"','href="/missing/"'),
        'report-link':('href="/reports/2026-W46/"','href="/missing/"'),
        'hidden':('<article data-workload=','<article hidden data-workload='),
        'css':('</style>','article{display:none}</style>'),
        'article':('"@type": "Article"','"@type": "FAQPage"'),
        'table-units':('USD per GPU-hour and','EUR per GPU-hour and'),
        'anchor':('href="#quote-0"','href="#quote-99"'),
        'tax':('Tax unknown','Tax exempt'),
        'instance':('data-format="instance-money">$8.00<','data-format="instance-money">$4.00<'),
        'billing':('data-format="minimum-charge-money">','data-format="minimum-charge-money">9'),
        'unmarked':('</article>','<p>Only $0.001 everywhere</p></article>'),
        'wording':('</article>','<p>AI pricing</p></article>'),
        'explanation':('data-search="workload:minima_note"','data-search="workload:wrong"'),
        'headroom':('comfortable (estimate)','unlimited memory'),
        'floor':('compute capability ≥ 7.5','compute capability ≥ 5.2'),
        'badge':('Arm (aarch64) host','x86_64 host'),
        'description':('GB estimate;','GB guaranteed;'),
        'duplicate':('noindex','index'),
        'unreviewed':('Future GPU</p></details>','Missing GPU</p></details>'),
    }
    if damage=='row': changed=re.sub(r'<tr><td>.*?</tr>','',raw,count=1)
    else:
        old,new=changes[damage];changed=raw.replace(old,new,1)
    if damage=='duplicate' and changed==raw:
        changed=raw.replace('<meta name="description"','<meta name="robots" content="noindex"><meta name="description"',1)
    assert changed!=raw,damage
    facts=_snapshot(store.connection,DAY,STAMP);errors=[]
    try:
        page=Page(changed)
        workload_audit.audit_page(page,changed,slug,workload_audit.snapshot(facts),facts,
            load_catalogs()['en'],ORIGIN,model_slugs(facts['models']),filename,errors)
    except AuditError: return
    assert errors,damage
