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
from pipelines.gpu_prices.workload_specs import WORKLOADS, WORKLOAD_FAMILIES, MODEL_CONFIGS, SAME_ARCHITECTURE, SOURCES, route, WORKLOAD_CAPACITY, GPU_SOFTWARE, VLLM_ROCM_TARGETS

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
    assert {s:d['required'] for s,d in data.items() if s.startswith('llama-') or s=='whisper-large'}==pytest.approx({
        MEDIUM:20.273741824,SMALL:7.7649534976,'llama-2-13b-fp16':34.5554432,
        'llama-2-13b-4bit':11.7452672,LARGE:170.68435456,
        'llama-3-1-70b-4bit':48.4667779072,'whisper-large':10})
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


# Reviewed official configs and BF16 index metadata, 2026-11-14. Each page
# links its upstream card/config/index; these independent expected values keep
# rounded marketing parameter counts out of arithmetic. No network in tests.
@pytest.mark.parametrize('model,parameters,layers,heads,kv_heads,hidden,intermediate,vocab,weight_bytes,cache,fp16,fourbit',[
    ('qwen2-5-7b',7615616512,28,28,4,3584,18944,152064,15231233024,.469762048,18.7472416768,7.0011225088),
    ('qwen2-5-14b',14770033664,48,40,8,5120,13824,152064,29540067328,1.610612736,37.0586935296,13.2754765824),
    ('qwen2-5-32b',32763876352,64,40,8,5120,27648,152064,65527752704,2.147483648,80.7807868928,24.6086531072),
    ('qwen2-5-72b',72706203648,80,64,8,8192,29568,152064,145412407296,2.68435456,177.1792433152,50.7926265856),
    ('mistral-7b-instruct-v0-3',7248023552,32,32,8,4096,14336,32768,14496047104,1.073741824,18.4689983488,5.905739776),
])
def test_exact_new_model_inputs_and_fixed_context(model,parameters,layers,heads,kv_heads,hidden,intermediate,vocab,weight_bytes,cache,fp16,fourbit):
    d=workloads.snapshot({})
    for precision,need in (('fp16',fp16),('4bit',fourbit)):
        slug=model+'-'+precision;s=WORKLOADS[slug]
        assert (s['parameter_count'],s['layers'],s['heads'],s['kv_heads'],s['head_dim'])==(parameters,layers,heads,kv_heads,128)
        assert (s['hidden'],s['intermediate'],s['vocab'])==(hidden,intermediate,vocab)
        assert s['max_context']==32768 and s['context']==8192 and s['batch']==1 and s['kv_bytes']==2
        assert s['parameters']==parameters/1e9 and parameters*2==weight_bytes
        # Reconstruct all trainable shapes, including Qwen's attention biases.
        k=kv_heads*128
        assert 2*vocab*hidden+layers*(2*hidden**2+2*hidden*k+3*hidden*intermediate+2*hidden+(hidden+2*k if model.startswith('qwen') else 0))+hidden==parameters
        assert d[slug]['required']==pytest.approx(need,abs=1e-10)
        assert d[slug]['kv']==pytest.approx(cache,abs=1e-12)
        assert {model+'_'+x for x in ('card','config','index')}<=set(s['sources'])
    assert d==workload_audit.snapshot({'models':{}})


def test_qwen_7b_tensor_parallel_four_heads_excludes_eight_gpus():
    rows=data_for([offer('RTX 3060 12GB','two',2,gpu_count=2,price_per_gpu_hour=1),
                   offer('RTX 3060 8GB','four',3,gpu_count=4,price_per_gpu_hour=.75),
                   offer('RTX 3060 8GB','eight',.01,gpu_count=8,price_per_gpu_hour=.00125)])
    d=workloads.snapshot(rows)
    assert {x['offer']['sku'] for x in d['qwen2-5-7b-fp16']['rows']['multi']}=={'two','four'}
    assert not d['qwen2-5-7b-4bit']['rows']['multi']
    assert d==workload_audit.snapshot({'models':rows})


def test_new_boundaries_change_the_fit_and_preserve_cheapest_instance_meta():
    rows=data_for([offer('A100 80GB','not-enough-for-qwen32',1),
                   offer('H100 NVL 94GB','single',7),
                   offer('L40S 48GB','tp',2,gpu_count=2,price_per_gpu_hour=1)])
    d=workloads.snapshot(rows);q=d['qwen2-5-32b-fp16']
    assert {x['offer']['sku'] for x in q['rows']['single']}=={'single'}
    assert q['overall']['on_demand'][1]['sku']=='single'
    assert q['heads']['multi']['on_demand'][1]['sku']=='tp'
    assert 'from $2.00/hr' in workloads.description('qwen2-5-32b-fp16',q,load_catalogs()['en'],DAY)
    assert d==workload_audit.snapshot({'models':rows})


def test_families_cover_seventeen_pages_once_and_aliases_have_no_routes():
    members=[s for group in WORKLOAD_FAMILIES.values() for s in group]
    assert len(members)==len(set(members))==len(WORKLOADS)==17
    assert set(members)==set(WORKLOADS)
    assert [len(m) for m in WORKLOAD_FAMILIES.values()]==[6,8,2,1]
    assert len(SAME_ARCHITECTURE)==3
    assert not any('deepseek' in s for s in WORKLOADS)
    for size in (7,14,32):
        for precision in ('fp16','4bit'):
            s=WORKLOADS[f'qwen2-5-{size}b-{precision}']
            assert s['same_as']==(f'deepseek_qwen_{size}b',)
            assert s['same_as'][0] in s['sources']
    for tr in load_catalogs().values():
        raw=workloads.entry_points(tr)
        assert raw.count('data-workload-family=')==4
        errors=[];workload_audit.audit_entry(Page(raw),raw,tr,'entries',errors)
        assert errors==[]
        damaged=raw.replace('data-workload-family="qwen"','data-workload-family="llama"')
        workload_audit.audit_entry(Page(damaged),damaged,tr,'entries',errors)
        assert errors


def test_vendor_numbers_units_and_context_are_cited_and_screened(site):
    _,out,_=site
    expected={7:(15.38,6.52,6.39,1),14:(29.50,11.36,11.22,1),32:(63.72,21.09,20.81,1),72:(140,42.50,42.13,2)}
    for size,(bf16,gptq,awq,n) in expected.items():
        c=MODEL_CONFIGS[f'qwen2-5-{size}b']
        assert c['vendor_bf16']==(bf16,n) and c['vendor_gptq']==(gptq,1) and c['vendor_awq']==(awq,1)
    for tr in load_catalogs().values():
        for slug,spec in WORKLOADS.items():
            raw=(out/tr.language.prefix/(route(slug).lstrip('/')+'index.html')).read_text(encoding='utf-8')
            p=Page(raw)
            if 'parameter_count' in spec:
                assert str(spec['parameter_count']) in p.search_values['workload:exact_inputs']
                assert '8192' in p.search_values['workload:exact_inputs'] and '32768' in p.search_values['workload:exact_inputs']
            if slug.startswith('qwen'):
                assert '6144' in p.search_values['workload:vendor_context'] and '2048' in p.search_values['workload:vendor_context']
                assert '1024³' in p.search_values['workload:vendor_context']
                assert 'workload:vendor_screen' in p.search_values
                assert ('workload:vendor_smaller' in p.search_values)==(spec['bits']==16 and size_from_slug(slug) in (32,72))
                key='workload:vendor_bf16' if spec['bits']==16 else 'workload:vendor_4bit'
                assert key in p.search_values
                assert ('workload:same_estimate' in p.search_values)==(spec.get('same_as')!=())
            else: assert 'workload:vendor_context' not in p.search_values
            if slug.startswith('mistral-'):
                value=p.search_values['workload:vendor_mistral']
                assert all(v in value for v in ('20 GB','5 GB','BF16','FP4','32k','8192'))
                assert 'mistral_overview' in spec['sources']


def size_from_slug(slug):
    return int(slug.split('-')[2][:-1])


@pytest.mark.parametrize('slug,vocab,hidden,u',[
    ('llama-3-1-8b-4bit',128256,4096,1050673152),
    ('llama-2-13b-4bit',32000,5120,327680000),
    ('llama-3-1-70b-4bit',128256,8192,2101346304),
    ('qwen2-5-7b-4bit',152064,3584,1089994752),
    ('qwen2-5-14b-4bit',152064,5120,1557135360),
    ('qwen2-5-32b-4bit',152064,5120,1557135360),
    ('qwen2-5-72b-4bit',152064,8192,2491416576),
    ('mistral-7b-instruct-v0-3-4bit',32768,4096,268435456),
])
def test_official_unquantized_inputs_and_rendered_split(site,slug,vocab,hidden,u):
    _,out,_=site;s=WORKLOADS[slug];d=workloads.snapshot({})[slug]
    assert (s['vocab'],s['hidden'],s['tie_word_embeddings'])==(vocab,hidden,False)
    assert d['unquantized_parameters']==u
    assert d['unquantized_weights']==pytest.approx(u*2/1e9)
    assert d['weights']>s['parameters']*.5
    for tr in load_catalogs().values():
        raw=(out/tr.language.prefix/(route(slug).lstrip('/')+'index.html')).read_text(encoding='utf-8')
        p=Page(raw);split=p.search_values['workload:weight_rule_4bit']
        assert str(u) in split and str(vocab) in split and str(hidden) in split
        assert 'workload:weight_rule' not in p.search_values
        assert 'workload:weight_inputs' in p.search_values
        if slug.startswith('qwen'):
            assert all('workload:checkpoint_'+fmt in p.search_values for fmt in ('awq','gptq'))
        if slug.startswith('llama'):
            assert 'workload:rounded_parameters' in p.search_values


def test_tied_embeddings_are_counted_once(monkeypatch):
    spec=dict(WORKLOADS[SMALL],tie_word_embeddings=True)
    monkeypatch.setitem(WORKLOADS,SMALL,spec)
    d=workloads.snapshot({})[SMALL]
    assert d['unquantized_parameters']==525336576
    assert d['weights']==pytest.approx(4.788004864)
    assert d==workload_audit.snapshot({'models':{}})[SMALL]


def test_corrected_live_llama_and_qwen_capacity_boundaries():
    models=data_for([offer('RTX 3050 6GB','six'),offer('RTX 4060 8GB','seven',vram_raw='7 GB'),
        offer('RTX 3060 8GB','eight'),offer('RTX 2080 Ti 11GB','eleven'),
        offer('RTX 3060 12GB','twelve'),offer('RTX A4000 16GB','sixteen'),
        offer('L40S 48GB','fortyeight'),offer('A100 80GB','eighty')])
    d=workloads.snapshot(models)
    assert {x['offer']['sku'] for x in d[SMALL]['rows']['single']}=={'eight','eleven','twelve','sixteen','fortyeight','eighty'}
    assert {x['offer']['sku'] for x in d['llama-2-13b-4bit']['rows']['single']}=={'twelve','sixteen','fortyeight','eighty'}
    assert {x['offer']['sku'] for x in d['llama-3-1-70b-4bit']['rows']['single']}=={'eighty'}
    assert 'seven' not in {x['offer']['sku'] for x in d['qwen2-5-7b-4bit']['rows']['single']}
    assert 'twelve' not in {x['offer']['sku'] for x in d['qwen2-5-14b-4bit']['rows']['single']}
    assert d==workload_audit.snapshot({'models':models})


def test_vendor_peak_vetoes_single_and_tp_after_gib_conversion(monkeypatch):
    # Exercise the vendor-dominant branch: today's corrected analytical
    # estimates happen to exceed every cited peak, so fixtures alone miss it.
    slug='qwen2-5-7b-4bit'
    monkeypatch.setitem(WORKLOADS,slug,dict(WORKLOADS[slug],vendor_gptq=(8,1),vendor_awq=(9,1)))
    models=data_for([offer('RTX 3060 8GB','eight'),offer('RTX 3080 10GB','ten'),
        offer('RTX 3050 6GB','tp',gpu_count=2),offer('RTX 4060 8GB','short-tp',gpu_count=2,vram_raw='4 GB')])
    d=workloads.snapshot(models);q=d[slug]
    assert q['vendor_gb']==pytest.approx(9.663676416)
    assert q['required']==q['vendor_gb']>q['estimate']
    assert {x['offer']['sku'] for x in q['rows']['single']}=={'ten'}
    assert {x['offer']['sku'] for x in q['rows']['multi']}=={'tp'}
    assert d==workload_audit.snapshot({'models':models})


def test_no_fit_is_below_the_cited_vendor_peak():
    models=data_for(population()+[offer('RTX 3050 6GB','six'),offer('RTX 2080 Ti 11GB','eleven'),offer('RTX 3060 12GB','twelve')])
    for slug,q in workloads.snapshot(models).items():
        if q['vendor_gb'] is None: continue
        for section in ('single','memory_only','multi'):
            for x in q['rows'][section]:
                capacity=x['vram']*(x['offer']['gpu_count'] if section=='multi' else 1)
                assert capacity>=q['vendor_gb']
                assert capacity>=q['estimate']


def test_vendor_screen_requires_matching_context_and_batch(monkeypatch):
    slug='qwen2-5-7b-4bit'
    monkeypatch.setitem(WORKLOADS,slug,dict(WORKLOADS[slug],context=4096))
    d=workloads.snapshot({})
    assert d[slug]['vendor_gb'] is None and d[slug]['required']==d[slug]['estimate']
    assert d==workload_audit.snapshot({'models':{}})


@pytest.mark.parametrize('old,new',[
    ('1089994752</span>','1</span>'),
    ('5570747392 bytes','1 bytes'),
    ('href="'+SOURCES['qwen2-5-7b_awq_index'][1]+'"','href="https://example.test/fake-index"'),
    ('Governing capacity screen: max','Governing capacity screen: min'),
])
def test_independent_audit_rejects_split_index_and_vendor_screen_corruption(site,old,new):
    store,out,_=site;slug='qwen2-5-7b-4bit';filename=route(slug).lstrip('/')+'index.html'
    raw=(out/filename).read_text(encoding='utf-8');changed=raw.replace(old,new,1)
    assert changed!=raw
    facts=_snapshot(store.connection,DAY,STAMP);errors=[]
    workload_audit.audit_page(Page(changed),changed,slug,workload_audit.snapshot(facts),facts,
        load_catalogs()['en'],ORIGIN,model_slugs(facts['models']),filename,errors)
    assert errors


@pytest.mark.parametrize('damage',['exact-input','vendor-number','vendor-link','alias-label','alias-link','mistral-number','mistral-link'])
def test_independent_audit_rejects_new_model_evidence_corruption(site,damage):
    store,out,_=site;slug='mistral-7b-instruct-v0-3-fp16' if damage.startswith('mistral-') else 'qwen2-5-7b-fp16';filename=route(slug).lstrip('/')+'index.html'
    raw=(out/filename).read_text(encoding='utf-8')
    old,new={
        'exact-input':('7615616512 parameters','7000000000 parameters'),
        'vendor-number':('15.38 GiB','1.00 GiB'),
        'vendor-link':('href="'+SOURCES['qwen_benchmark'][1]+'"','href="https://example.test/fake"'),
        'alias-label':('DeepSeek-R1-Distill-Qwen-7B</a>','DeepSeek-R1-Distill-Qwen-14B</a>'),
        'alias-link':('href="'+SOURCES['deepseek_qwen_7b'][1]+'"','href="https://example.test/fake"'),
        'mistral-number':('20 GB at BF16','2 GB at BF16'),
        'mistral-link':('href="'+SOURCES['mistral_overview'][1]+'"','href="https://example.test/fake"'),
    }[damage]
    changed=raw.replace(old,new,1);assert changed!=raw
    facts=_snapshot(store.connection,DAY,STAMP);errors=[]
    workload_audit.audit_page(Page(changed),changed,slug,workload_audit.snapshot(facts),facts,
        load_catalogs()['en'],ORIGIN,model_slugs(facts['models']),filename,errors)
    assert errors,damage


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
    assert result['audit']['workload_pages']==17*8
    indexed={n.text for n in ET.parse(out/'sitemap.xml').findall('.//{*}loc')}
    for lang in LANGUAGES:
        localized={n.text for n in ET.parse(out/('sitemap-'+lang.code+'.xml')).findall('.//{*}loc')}
        linking=[Page((out/lang.prefix/path).read_text(encoding='utf-8')) for path in ('index.html','guides/index.html','guides/choose-gpu-cloud-llm-inference/index.html')]
        related_raw=(out/lang.prefix/'guides/choose-gpu-cloud-llm-inference/index.html').read_text(encoding='utf-8')
        related=re.findall(r'<h2 data-search="guide:related">.*?</h2><ul data-workload-related="true">(.*?)</ul><p>',related_raw,re.S)
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
        assert len(set(titles))==len(set(descriptions))==17
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
    assert d['llama-2-13b-fp16']['overall']['on_demand'][1]['gpu_model']=='RTX A6000 48GB'
    assert d['llama-3-1-70b-4bit']['overall']['on_demand'][1]['gpu_model']=='A100 80GB'
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
    assert d[SMALL]['siblings']==[dict(slug='qwen2-5-14b-4bit',sections=['single'])]
    for slug in (MEDIUM,'qwen2-5-14b-4bit','qwen2-5-32b-fp16','qwen2-5-14b-fp16','qwen2-5-72b-fp16','whisper-large'): assert d[slug]['indexable']
    for slug in ('llama-2-13b-4bit','qwen2-5-32b-4bit','qwen2-5-72b-4bit','llama-2-13b-fp16','llama-3-1-70b-4bit',LARGE): assert not d[slug]['indexable']
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


def test_audit_policy_cannot_be_disabled_by_manifest(site,gpu_site_copy):
    store,out,_=gpu_site_copy(site);p=out/'gpu-prices-manifest.json';original=p.read_text(encoding='utf-8')
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
        'rule':('data-format="decimal3">7.765<','data-format="decimal3">7.766<'),
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
